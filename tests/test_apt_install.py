"""Behaviour of the apt-install action's retries.

`retry.sh` is sourced as it ships, and the step that reads the package lists is read out of
`action.yml`, and both are run under bash as GitHub runs a composite step, with `-e` and
`pipefail`, against stand-ins for `sudo`, `timeout`, `sleep` and `apt-get` on PATH.  Each
stand-in records its call.  What this cannot test: a real mirror, the cache, and the install
step's check of cached files against the index, which reads apt's own archive directory.
"""

import os
import pathlib
import stat
import subprocess
import textwrap

import pytest
import yaml

ROOT = pathlib.Path(__file__).resolve().parent.parent
ACTION = ROOT / "actions" / "apt-install"

# Fails its first STUB_<SUBCOMMAND>_FAILS calls of a subcommand with STUB_STATUS, then succeeds;
# a simulated install prints an Inst line for each package.
APT_GET = textwrap.dedent("""\
    #!/usr/bin/env bash
    sub="" simulate=0 packages=()
    for arg in "$@"; do
      case "$arg" in
        -s) simulate=1 ;;
        -o|-y|-q|--no-install-recommends|Acquire::*) ;;
        update|install) [ -z "$sub" ] && sub="$arg" || packages+=("$arg") ;;
        *) [ -n "$sub" ] && packages+=("$arg") ;;
      esac
    done
    if [ "$simulate" = 1 ]; then
      for p in "${packages[@]}"; do echo "Inst $p (1.0-1 Ubuntu:24.04/noble [amd64])"; done
      exit 0
    fi
    echo "apt-get $*" >> "$STUB_DIR/calls"
    count_file="$STUB_DIR/$sub.count"
    count=$(( $(cat "$count_file" 2>/dev/null || echo 0) + 1 ))
    echo "$count" > "$count_file"
    fails_var="STUB_${sub^^}_FAILS"
    if [ "$count" -le "${!fails_var:-0}" ]; then
      exit "${STUB_STATUS:-100}"
    fi
""")

SUDO = '#!/usr/bin/env bash\nexec "$@"\n'
TIMEOUT = '#!/usr/bin/env bash\necho "timeout $1" >> "$STUB_DIR/calls"\nshift\nexec "$@"\n'
SLEEP = '#!/usr/bin/env bash\necho "sleep $1" >> "$STUB_DIR/calls"\n'


@pytest.fixture
def stubs(tmp_path):
    for name, body in (("apt-get", APT_GET), ("sudo", SUDO), ("timeout", TIMEOUT), ("sleep", SLEEP)):
        path = tmp_path / name
        path.write_text(body)
        path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return tmp_path


def bash(script, stubs, **env):
    full = {
        "PATH": f"{stubs}:{os.environ['PATH']}",
        "STUB_DIR": str(stubs),
        "GITHUB_ACTION_PATH": str(ACTION),
        "RUNNER_TEMP": str(stubs),
        "RUNNER_ARCH": "X64",
        "GITHUB_OUTPUT": str(stubs / "output"),
        **{k: str(v) for k, v in env.items()},
    }
    result = subprocess.run(
        ["bash", "--noprofile", "--norc", "-eo", "pipefail", "-c", script],
        env=full, capture_output=True, text=True, timeout=30,
    )
    calls = (stubs / "calls").read_text().splitlines() if (stubs / "calls").exists() else []
    return result, calls


def install(stubs, **env):
    script = f'source "{ACTION}/retry.sh"\napt_retry "installing a b" apt_install_once a b\n'
    return bash(script, stubs, **env)


def step(name):
    document = yaml.safe_load((ACTION / "action.yml").read_text())
    (found,) = [s for s in document["runs"]["steps"] if s.get("name") == name]
    return found["run"]


PLAN = "Read the package lists, and name what apt will install"


def test_every_call_is_bounded_and_retries_its_downloads(stubs):
    result, calls = install(stubs)
    assert result.returncode == 0, result.stderr
    assert calls == [
        "timeout 900",
        "apt-get -o Acquire::Retries=3 install -y --no-install-recommends a b",
    ]


def test_a_failed_install_reads_the_lists_again_and_is_tried_again(stubs):
    # The stale index: a fresh read of the lists is what lets the next attempt succeed.
    result, calls = install(stubs, STUB_INSTALL_FAILS=2, STUB_STATUS=100)
    assert result.returncode == 0, result.stderr
    assert [c for c in calls if not c.startswith("timeout")] == [
        "apt-get -o Acquire::Retries=3 install -y --no-install-recommends a b",
        "apt-get -o Acquire::Retries=3 update",
        "sleep 15",
        "apt-get -o Acquire::Retries=3 install -y --no-install-recommends a b",
        "apt-get -o Acquire::Retries=3 update",
        "sleep 15",
        "apt-get -o Acquire::Retries=3 install -y --no-install-recommends a b",
    ]
    assert "installing a b failed with exit 100 on attempt 1 of 5; trying again in 15s" in result.stdout
    assert "on attempt 2 of 5" in result.stdout


def test_an_install_failing_every_attempt_fails_saying_what_and_how(stubs):
    result, calls = install(stubs, STUB_INSTALL_FAILS=99, STUB_STATUS=100)
    assert result.returncode == 100
    assert calls.count("apt-get -o Acquire::Retries=3 install -y --no-install-recommends a b") == 5
    # No wait after the last attempt.
    assert calls.count("sleep 15") == 4
    assert result.stderr.strip() == (
        "::error::apt-install: installing a b failed on all 5 attempts; the last exited 100, "
        "and apt's output above says why"
    )


def test_a_timeout_is_named_as_one(stubs):
    result, _ = install(stubs, STUB_INSTALL_FAILS=99, STUB_STATUS=124)
    assert result.returncode == 124
    assert "the last exited 124, a timeout, and" in result.stderr
    assert "failed with exit 124, a timeout, on attempt 1 of 5" in result.stdout


def test_a_read_of_the_lists_that_fails_does_not_end_an_attempt_early(stubs):
    # The re-read after a failed install is best effort: the install's own status decides.
    result, _ = install(stubs, STUB_INSTALL_FAILS=1, STUB_UPDATE_FAILS=99)
    assert result.returncode == 0, result.stderr


def test_the_plan_step_retries_reading_the_lists(stubs):
    result, calls = bash(step(PLAN), stubs, STUB_UPDATE_FAILS=2, PACKAGES="musl-tools lintian")
    assert result.returncode == 0, result.stderr
    assert calls.count("apt-get -o Acquire::Retries=3 update") == 3
    assert "reading the package lists failed with exit 100 on attempt 2 of 5" in result.stdout
    assert (stubs / "apt-install-plan").read_text() == "lintian 1.0-1 amd64\nmusl-tools 1.0-1 amd64\n"
    assert "key=apt-X64-musl-tools lintian-" in (stubs / "output").read_text()


def test_the_plan_step_fails_when_the_lists_cannot_be_read(stubs):
    result, _ = bash(step(PLAN), stubs, STUB_UPDATE_FAILS=99, PACKAGES="lintian")
    assert result.returncode == 100
    assert "reading the package lists failed on all 5 attempts" in result.stderr
    assert not (stubs / "apt-install-plan").exists()
