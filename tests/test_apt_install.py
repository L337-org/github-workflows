"""Behaviour of the apt-install action's retries.

`retry.sh` is sourced as it ships, and the action's steps are read out of `action.yml`, and all
are run under bash as GitHub runs a composite step, with `-e` and `pipefail`, against stand-ins
for `sudo`, `timeout`, `sleep`, `apt-get` and `apt-cache` on PATH.  Each stand-in records its
call.  What this cannot test, and the premerge job that runs the action on a runner does: a real
mirror and apt, the ownership of the archive directory, and the cache.
"""

import hashlib
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

# Ownership needs root, which a test does not have: recorded, not changed.
SUDO = textwrap.dedent("""\
    #!/usr/bin/env bash
    if [ "$1" = chown ]; then echo "sudo $*" >> "$STUB_DIR/calls"; exit 0; fi
    exec "$@"
""")

# The index's SHA-256 for PACKAGE:ARCH=VERSION, from the file the test wrote for it.
APT_CACHE = textwrap.dedent("""\
    #!/usr/bin/env bash
    printf 'Package: x\\nSHA256: %s\\n' "$(cat "$STUB_DIR/sha/$2")"
""")
TIMEOUT = '#!/usr/bin/env bash\necho "timeout $1" >> "$STUB_DIR/calls"\nshift\nexec "$@"\n'
SLEEP = '#!/usr/bin/env bash\necho "sleep $1" >> "$STUB_DIR/calls"\n'


@pytest.fixture
def stubs(tmp_path):
    stand_ins = (
        ("apt-get", APT_GET), ("apt-cache", APT_CACHE), ("sudo", SUDO), ("timeout", TIMEOUT),
        ("sleep", SLEEP),
    )
    for name, body in stand_ins:
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
        "APT_INSTALL_ROOT": str(stubs / "archives"),
        "USER": "runner",
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


def outputs(stubs):
    return dict(line.split("=", 1) for line in (stubs / "output").read_text().splitlines())


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
    found = outputs(stubs)
    assert pathlib.Path(found["plan"]).read_text() == "lintian 1.0-1 amd64\nmusl-tools 1.0-1 amd64\n"
    assert found["key"].startswith(found["prefix"])


def test_the_plan_step_fails_when_the_lists_cannot_be_read(stubs):
    result, _ = bash(step(PLAN), stubs, STUB_UPDATE_FAILS=99, PACKAGES="lintian")
    assert result.returncode == 100
    assert "reading the package lists failed on all 5 attempts" in result.stderr
    assert not list(stubs.glob("apt-install-plan-*"))


def test_packages_one_to_a_line_are_the_same_as_on_one(stubs, tmp_path_factory):
    # A YAML block scalar gives them one to a line, with a trailing line break.
    result, _ = bash(step(PLAN), stubs, PACKAGES="musl-tools\nlintian\n")
    assert result.returncode == 0, result.stderr
    lines = outputs(stubs)
    other = tmp_path_factory.mktemp("other")
    for name in ("apt-get", "apt-cache", "sudo", "timeout", "sleep"):
        (other / name).write_bytes((stubs / name).read_bytes())
        (other / name).chmod(0o755)
    result, _ = bash(step(PLAN), other, PACKAGES="lintian   musl-tools")
    assert result.returncode == 0, result.stderr
    spaced = outputs(other)
    assert lines["prefix"] == spaced["prefix"]
    assert lines["key"] == spaced["key"]
    assert pathlib.Path(lines["plan"]).read_text().count("\n") == 2


def test_no_packages_is_an_error(stubs):
    result, calls = bash(step(PLAN), stubs, PACKAGES=" \n ")
    assert result.returncode == 1
    assert result.stderr.strip() == "::error::apt-install: the packages input names no package"
    assert calls == []


def test_each_list_of_packages_downloads_into_a_directory_of_its_own(stubs):
    result, calls = bash(step(PLAN), stubs, PACKAGES="lintian")
    assert result.returncode == 0, result.stderr
    first = outputs(stubs)["archives"]
    (stubs / "output").unlink()
    result, _ = bash(step(PLAN), stubs, PACKAGES="musl-tools")
    assert result.returncode == 0, result.stderr
    second = outputs(stubs)["archives"]
    assert first != second
    for archives in (first, second):
        assert pathlib.Path(archives, "partial").is_dir()
    assert f"sudo chown runner {first}" in calls


INSTALL = "Install"


def deb(directory, package, version, arch, content):
    """A cached archive file as apt names it, and its SHA-256."""
    name = f"{package}_{version.replace(':', '%3a')}_{arch}.deb"
    (directory / name).write_bytes(content)
    return hashlib.sha256(content).hexdigest()


def test_the_install_step_keeps_only_cached_files_the_index_signs_for(stubs):
    archives = stubs / "archives" / "x"
    archives.mkdir(parents=True)
    plan = stubs / "plan"
    plan.write_text("good 2:1.0-1 amd64\nbad 1.0-1 all\n")
    (stubs / "sha").mkdir()
    good = deb(archives, "good", "2:1.0-1", "amd64", b"good")
    (stubs / "sha" / "good:amd64=2:1.0-1").write_text(good)
    deb(archives, "bad", "1.0-1", "all", b"tampered")
    (stubs / "sha" / "bad:all=1.0-1").write_text("0" * 64)
    deb(archives, "old", "0.9-1", "amd64", b"old")
    result, calls = bash(
        step(INSTALL), stubs, PACKAGES="good bad", KEY="k", HIT="true",
        APT_ARCHIVES=archives, PLAN=plan,
    )
    assert result.returncode == 0, result.stderr
    assert sorted(p.name for p in archives.iterdir()) == ["good_2%3a1.0-1_amd64.deb"]
    assert "apt-install: 1 cached package files match the index; 1 older ones deleted unused" in result.stdout
    assert "::warning::apt-install: deleting cached bad_1.0-1_all.deb: its SHA-256 is" in result.stdout
    assert "gh cache delete k" in result.stdout
    assert (
        f"apt-get -o Acquire::Retries=3 -o Dir::Cache::archives={archives}/ install -y "
        "--no-install-recommends good bad"
    ) in calls


def test_the_install_step_warns_when_nothing_is_left_to_cache(stubs):
    archives = stubs / "archives" / "x"
    archives.mkdir(parents=True)
    plan = stubs / "plan"
    plan.write_text("")
    result, _ = bash(
        step(INSTALL), stubs, PACKAGES="lintian", KEY="k", HIT="false",
        APT_ARCHIVES=archives, PLAN=plan,
    )
    assert result.returncode == 0, result.stderr
    assert f"no package file is left in {archives}, so nothing will be cached" in result.stdout
