"""Behaviour of the release preflight and attach actions, and the cancelled-release workflow.

Each step's script is read out of its action or workflow file and run under bash as GitHub
runs a step, with `-e` and `pipefail`, with the environment GitHub would give it.  The
stand-ins for `gh` and `curl` share a fake release on disk - `assets` lists each asset as
`ID NAME STATE`, and `asset-ID` holds its bytes - so an upload adds to it, a delete takes from
it, and a download reads it.  What this cannot test: the GitHub expressions that fill the
environment, and GitHub itself.
"""

import os
import pathlib
import stat
import subprocess
import textwrap

import pytest
import yaml

ROOT = pathlib.Path(__file__).resolve().parent.parent

GH = textwrap.dedent("""\
    #!/usr/bin/env bash
    echo "gh $*" >> "$STUB_DIR/calls"
    args=("$@")
    last="${args[-1]}"
    case "$*" in
      *"--jq .prerelease"*)
        [ "${STUB_GH_EXIT:-0}" = 0 ] || { echo "gh: HTTP 404" >&2; exit 1; }
        echo "$STUB_PRERELEASE" ;;
      *"--method DELETE"*)
        id="${last##*/}"
        awk -v id="$id" '$1 != id' "$STUB_DIR/assets" > "$STUB_DIR/assets.new"
        mv "$STUB_DIR/assets.new" "$STUB_DIR/assets" ;;
      *"application/octet-stream"*) cat "$STUB_DIR/asset-${last##*/}" ;;
      *"/assets --paginate"*) cat "$STUB_DIR/assets" ;;
      *) echo "unexpected gh call: $*" >&2; exit 2 ;;
    esac
""")

CURL = textwrap.dedent("""\
    #!/usr/bin/env bash
    url="${@: -1}"
    while [ $# -gt 1 ]; do
      case "$1" in
        --data-binary) data="${2#@}"; shift ;;
        --output) out="$2"; shift ;;
        -H) echo "$2" >> "$STUB_DIR/curl.headers"; shift ;;
      esac
      shift
    done
    encoded="${url##*name=}"
    name="$(printf '%b' "${encoded//%/\\\\x}")"
    echo "upload $encoded" >> "$STUB_DIR/calls"
    if [ -n "${STUB_CURL_FAIL:-}" ]; then
      printf '%s' "$STUB_CURL_FAIL" > "$out"
      exit 22
    fi
    # A counter, never reused, as GitHub's ids are not after a delete.
    id=$(( $(cat "$STUB_DIR/next-id" 2>/dev/null || echo 100) + 1 ))
    echo "$id" > "$STUB_DIR/next-id"
    echo "$id $name uploaded" >> "$STUB_DIR/assets"
    if [ -n "${STUB_CORRUPT:-}" ]; then echo corrupt > "$STUB_DIR/asset-$id"; else cp "$data" "$STUB_DIR/asset-$id"; fi
    echo '{}' > "$out"
""")


@pytest.fixture
def stubs(tmp_path):
    for name, body in (("gh", GH), ("curl", CURL)):
        path = tmp_path / name
        path.write_text(body)
        path.chmod(path.stat().st_mode | stat.S_IXUSR)
    (tmp_path / "assets").write_text("")
    return tmp_path


def script(relative, name=None):
    document = yaml.safe_load((ROOT / relative).read_text())
    if "runs" in document:
        steps = document["runs"]["steps"]
    else:
        (job,) = document["jobs"].values()
        steps = job["steps"]
    (found,) = [s for s in steps if "run" in s and (name is None or s.get("name") == name)]
    return found["run"]


def run(relative, stubs, **env):
    full = {
        "PATH": f"{stubs}:{os.environ['PATH']}",
        "STUB_DIR": str(stubs),
        "RUNNER_TEMP": str(stubs),
        "GITHUB_SHA": "abc123",
        "REPO": "L337-org/thing",
        "GH_TOKEN": "token",
        **{k: str(v) for k, v in env.items()},
    }
    result = subprocess.run(
        ["bash", "--noprofile", "--norc", "-eo", "pipefail", "-c", script(relative)],
        env=full, capture_output=True, text=True, timeout=30, cwd=stubs,
    )
    calls = (stubs / "calls").read_text().splitlines() if (stubs / "calls").exists() else []
    return result, calls


PREFLIGHT = "actions/release-preflight/action.yml"


def preflight(stubs, **env):
    base = {
        "EVENT": "release", "OWNER": "L337-org", "TAG": "v1.2.3", "RELEASE_ID": "7",
        "VERSION": "1.2.3", "PREFIX": "v",
    }
    return run(PREFLIGHT, stubs, **{**base, **env})


def test_a_release_tagged_with_the_declared_version_passes(stubs):
    result, calls = preflight(stubs)
    assert result.returncode == 0, result.stderr
    assert "release-preflight: releasing 1.2.3, tagged v1.2.3, from abc123" in result.stdout
    # A major version above 0 does not ask whether it is a pre-release.
    assert calls == []


def test_the_prefix_defaults_to_v_and_an_empty_one_is_held_by_the_probe():
    # A repository with bare tags passes tag-prefix: '', which works only because an input given
    # as empty overrides its default: the input-defaults premerge job holds that on GitHub.
    document = yaml.safe_load((ROOT / PREFLIGHT).read_text())
    assert document["inputs"]["tag-prefix"]["default"] == "v"
    probe = yaml.safe_load((ROOT / ".github" / "actions" / "input-default-probe" / "action.yml").read_text())
    assert probe["inputs"]["value"]["default"] == "v"
    premerge = yaml.safe_load((ROOT / ".github" / "workflows" / "premerge.yaml").read_text())
    steps = premerge["jobs"]["input-defaults"]["steps"]
    assert {"value": "", "expected": ""} in [s.get("with") for s in steps]


def test_a_two_part_version_with_no_prefix_passes(stubs):
    result, _ = preflight(stubs, TAG="6.2", VERSION="6.2", PREFIX="")
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(
    "env, message",
    [
        ({"EVENT": "push"}, "checks a release, but this run was started by 'push'"),
        ({"OWNER": "someone"}, "published from repository owner 'someone', not L337-org"),
        ({"VERSION": ""}, "the version input is empty"),
        ({"TAG": "1.2.3"}, "the release's tag is '1.2.3', but the repository declares 1.2.3, so the tag must be 'v1.2.3'"),
        ({"TAG": "v6.2.0", "VERSION": "6.2"}, "so the tag must be 'v6.2'"),
    ],
)
def test_preflight_refuses_naming_why(stubs, env, message):
    result, _ = preflight(stubs, **env)
    assert result.returncode == 1
    assert message in result.stderr


def test_a_major_version_of_0_must_be_a_pre_release(stubs):
    result, calls = preflight(stubs, TAG="v0.4.0", VERSION="0.4.0", STUB_PRERELEASE="false")
    assert result.returncode == 1
    assert (
        "0.4.0 has a major version of 0, so its release must be marked as a pre-release; edit "
        "release 7, tick pre-release, and re-run this workflow"
    ) in result.stderr
    assert calls == ["gh api repos/L337-org/thing/releases/7 --jq .prerelease"]
    result, _ = preflight(stubs, TAG="v0.4.0", VERSION="0.4.0", STUB_PRERELEASE="true")
    assert result.returncode == 0, result.stderr


def test_a_release_that_cannot_be_read_fails_saying_so(stubs):
    result, _ = preflight(stubs, TAG="v0.4.0", VERSION="0.4.0", STUB_GH_EXIT=1)
    assert result.returncode == 1
    assert "could not read release 7 of L337-org/thing to see whether it is marked pre-release" in result.stderr


ATTACH = "actions/release-attach/action.yml"


def built(stubs, name, content):
    (stubs / name).write_bytes(content)
    return name


def attach(stubs, files, **env):
    return run(ATTACH, stubs, FILES=files, RELEASE_ID="7", **env)


def served(stubs):
    lines = (stubs / "assets").read_text().splitlines()
    return {line.split()[1]: (stubs / f"asset-{line.split()[0]}").read_bytes() for line in lines}


def test_each_file_is_attached_and_checked(stubs):
    built(stubs, "x_1.0_amd64.deb", b"amd")
    built(stubs, "x_1.0_arm64.deb", b"arm")
    result, calls = attach(stubs, "x_1.0_amd64.deb\nx_1.0_arm64.deb\n")
    assert result.returncode == 0, result.stderr
    assert served(stubs) == {"x_1.0_amd64.deb": b"amd", "x_1.0_arm64.deb": b"arm"}
    assert "release-attach: x_1.0_amd64.deb served as built" in result.stdout
    assert "Content-Type: application/vnd.debian.binary-package" in (stubs / "curl.headers").read_text()
    # The token is in a header, never in the URL or on gh's command line.
    assert not any("token" in call for call in calls)


def test_a_glob_attaches_what_it_matches_and_one_matching_nothing_is_named(stubs):
    (stubs / "dist").mkdir()
    (stubs / "dist" / "a.deb").write_bytes(b"a")
    (stubs / "dist" / "b.deb").write_bytes(b"b")
    result, _ = attach(stubs, "dist/*.deb")
    assert result.returncode == 0, result.stderr
    assert served(stubs) == {"a.deb": b"a", "b.deb": b"b"}
    result, _ = attach(stubs, "dist/*.rpm")
    assert result.returncode == 1
    assert "dist/\\*.rpm does not exist, so nothing is attached" in result.stderr


def test_a_name_url_syntax_would_split_reaches_github_whole(stubs):
    built(stubs, "a&b#1%.deb", b"x")
    result, calls = attach(stubs, "a&b#1%.deb")
    assert result.returncode == 0, result.stderr
    assert "upload a%26b%231%25.deb" in calls
    assert served(stubs) == {"a&b#1%.deb": b"x"}


def test_an_identical_file_already_attached_is_left_as_it_is(stubs):
    built(stubs, "x.deb", b"same")
    (stubs / "assets").write_text("5 x.deb uploaded\n")
    (stubs / "asset-5").write_bytes(b"same")
    result, calls = attach(stubs, "x.deb")
    assert result.returncode == 0, result.stderr
    assert "x.deb is already attached and identical; left as it is" in result.stdout
    assert not any(call.startswith("upload") for call in calls)


def test_a_different_file_left_by_an_earlier_run_is_replaced(stubs):
    built(stubs, "x.deb", b"new")
    (stubs / "assets").write_text("5 x.deb uploaded\n")
    (stubs / "asset-5").write_bytes(b"old")
    result, calls = attach(stubs, "x.deb")
    assert result.returncode == 0, result.stderr
    assert "::warning::release-attach: x.deb is already attached and differs from this build" in result.stdout
    assert "gh api --method DELETE repos/L337-org/thing/releases/assets/5" in calls
    assert served(stubs) == {"x.deb": b"new"}


def test_an_unfinished_upload_is_replaced(stubs):
    built(stubs, "x.deb", b"new")
    (stubs / "assets").write_text("5 x.deb starter\n")
    result, _ = attach(stubs, "x.deb")
    assert result.returncode == 0, result.stderr
    assert "an upload that never finished (starter), left by an earlier run; replacing it" in result.stdout
    assert served(stubs) == {"x.deb": b"new"}


def test_a_missing_file_attaches_nothing(stubs):
    built(stubs, "x.deb", b"x")
    result, calls = attach(stubs, "x.deb y.deb")
    assert result.returncode == 1
    assert "y.deb does not exist, so nothing is attached" in result.stderr
    assert calls == []


def test_a_refused_upload_fails_with_githubs_answer(stubs):
    built(stubs, "x.deb", b"x")
    result, _ = attach(stubs, "x.deb", STUB_CURL_FAIL='{"message":"Validation Failed"}')
    assert result.returncode == 1
    assert 'uploading x.deb to release 7 failed; GitHub said: {"message":"Validation Failed"}' in result.stderr


def test_a_release_serving_something_else_fails(stubs):
    built(stubs, "x.deb", b"x")
    result, _ = attach(stubs, "x.deb", STUB_CORRUPT="1")
    assert result.returncode == 1
    assert "release 7 serves a x.deb that differs from the one built" in result.stderr


@pytest.mark.parametrize(
    "files, release, message",
    [(" \n", "7", "the files input names no file"), ("x.deb", "", "no release to attach to")],
)
def test_attach_refuses_what_it_cannot_do(stubs, files, release, message):
    built(stubs, "x.deb", b"x")
    result, _ = run(ATTACH, stubs, FILES=files, RELEASE_ID=release)
    assert result.returncode == 1
    assert message in result.stderr


def test_a_cancelled_release_fails_naming_the_job(stubs):
    result, _ = run(".github/workflows/release-cancelled.yaml", stubs, JOB="build-and-release")
    assert result.returncode == 1
    assert "The build-and-release job ended as cancelled" in result.stderr
