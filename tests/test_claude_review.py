"""Behaviour of the Claude review's posting step, and what the review session is offered.

The step's script is read out of the workflow file and run under bash with the environment
GitHub would give it, against a stand-in for `gh` that keeps the comment it was asked to post.
So what is tested is the script that ships, not a copy of it.  What this cannot test: the GitHub
expressions that fill the environment, and the review session itself.
"""

import json
import os
import pathlib
import shlex
import stat
import subprocess
import textwrap

import pytest
import yaml

ROOT = pathlib.Path(__file__).resolve().parent.parent
WORKFLOW = ROOT / ".github" / "workflows" / "claude-review.yaml"
HEAD = "a" * 40

# Keeps the file `gh pr comment` was handed as the comment it would post.
GH = textwrap.dedent("""\
    #!/usr/bin/env bash
    while [ $# -gt 0 ]; do
      case "$1" in
        --body-file) cp "$2" "$STUB_DIR/comment.md"; shift ;;
      esac
      shift
    done
""")


def review_step(name):
    document = yaml.safe_load(WORKFLOW.read_text())
    (step,) = [s for s in document["jobs"]["review"]["steps"] if s.get("name") == name]
    return step


@pytest.fixture
def stubs(tmp_path):
    gh = tmp_path / "gh"
    gh.write_text(GH)
    gh.chmod(gh.stat().st_mode | stat.S_IXUSR)
    return tmp_path


def post(stubs, review):
    env = {
        "PATH": f"{stubs}:{os.environ['PATH']}",
        "STUB_DIR": str(stubs),
        "TMPDIR": str(stubs),
        "PR": "7",
        "GITHUB_REPOSITORY": "L337-org/example",
        "REVIEW_JSON": json.dumps(review),
        "EXECUTION_FILE": "",
        "HEAD_SHA": HEAD,
        "MODE": "full",
        "FROM": "b" * 40,
        "COUNT": "0",
        "TRIGGER": "other",
        "RUN_URL": "https://github.com/L337-org/example/actions/runs/1",
    }
    result = subprocess.run(
        ["bash", "-c", review_step("Post the review")["run"]], env=env, capture_output=True,
        text=True, timeout=30,
    )
    comment = stubs / "comment.md"
    return result, comment.read_text() if comment.exists() else None


def test_a_complete_review_is_posted_with_its_marker(stubs):
    result, comment = post(stubs, {"verdict": "approve", "complete": True, "review": "Fine."})
    assert result.returncode == 0, result.stderr
    assert comment.startswith("## Claude review: approve\n")
    assert "Fine." in comment
    assert f"reviewed-sha={HEAD} depth=full" in comment


@pytest.mark.parametrize("review", [
    {"verdict": "approve", "complete": False, "review": "Read half of it."},
    {"verdict": "changes_requested", "complete": False, "review": "Read half of it."},
    # A session that leaves the field out cannot be told from one that skipped files.
    {"verdict": "approve", "review": "Read half of it."},
])
def test_an_incomplete_review_is_posted_but_fails_and_carries_no_marker(stubs, review):
    result, comment = post(stubs, review)
    assert result.returncode == 1
    assert "::error::The review did not cover every file in the range" in result.stdout
    assert comment.startswith("## Claude review: incomplete\n")
    assert f"its verdict on what it read was {review['verdict'].replace('_', ' ')}" in comment
    assert "Read half of it." in comment
    assert "reviewed-sha=" not in comment


def test_the_session_cannot_hand_work_to_other_agents_and_must_say_whether_it_finished():
    arguments = shlex.split(review_step("Review")["with"]["claude_args"].replace("\n", " "))
    disallowed = arguments[arguments.index("--disallowedTools") + 1].split(",")
    assert {"Agent", "Workflow"} <= set(disallowed)
    allowed = arguments[arguments.index("--allowedTools") + 1].split(",")
    assert not {"Agent", "Task", "Workflow"} & set(allowed)
    schema = json.loads(arguments[arguments.index("--json-schema") + 1])
    assert schema["properties"]["complete"] == {"type": "boolean"}
    assert "complete" in schema["required"]
