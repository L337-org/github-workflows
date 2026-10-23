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


# Answers `gh api` with the comments in $STUB_COMMENTS, one JSON object a line, as the step's
# `--jq '.[]'` would.
GH_COMMENTS = textwrap.dedent("""\
    #!/usr/bin/env bash
    printf '%s' "$STUB_COMMENTS"
""")

# `git merge-base` gives the base; `--is-ancestor` says yes.
GIT = textwrap.dedent("""\
    #!/usr/bin/env bash
    if [ "$1" = merge-base ] && [ "$2" = --is-ancestor ]; then exit 0; fi
    if [ "$1" = merge-base ]; then echo "$STUB_BASE"; exit 0; fi
    exit 1
""")

BASE = "c" * 40


def own(body, user="github-actions[bot]"):
    return {"user": {"login": user}, "body": body, "author_association": "NONE",
            "created_at": "2026-10-23T00:00:00Z"}


def decide(tmp_path, comments, push=False, limit=10):
    for name, script in (("gh", GH_COMMENTS), ("git", GIT)):
        path = tmp_path / name
        path.write_text(script)
        path.chmod(path.stat().st_mode | stat.S_IXUSR)
    output = tmp_path / "output"
    output.write_text("")
    env = {
        "PATH": f"{tmp_path}:{os.environ['PATH']}",
        "TMPDIR": str(tmp_path),
        "STUB_COMMENTS": "\n".join(json.dumps(c) for c in comments),
        "STUB_BASE": BASE,
        "GITHUB_OUTPUT": str(output),
        "GITHUB_REPOSITORY": "L337-org/example",
        "PR": "7",
        "HEAD_SHA": HEAD,
        "BASE_REF": "main",
        "DEPTH": "auto",
        "PUSH_TRIGGERED": "true" if push else "false",
        "MAX_AUTO_REVIEWS": str(limit),
        "FULL_EFFORT": "high",
        "DELTA_EFFORT": "medium",
        "FULL_MODEL": "opus",
        "DELTA_MODEL": "sonnet",
    }
    result = subprocess.run(
        ["bash", "-c", review_step("Read earlier rounds, and decide the depth and range")["run"]],
        env=env, capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stderr
    lines = output.read_text().splitlines()
    return dict(line.split("=", 1) for line in lines if "=" in line and "<<" not in line)


def test_a_review_at_the_head_leaves_nothing_to_review(tmp_path):
    marker = f"<!-- claude-review reviewed-sha={HEAD} depth=full trigger=other -->"
    review = own(f"## Claude review: approve\n\nFine.\n\n{marker}\n")
    assert decide(tmp_path, [review])["mode"] == "none"


def test_a_marker_quoted_in_an_incomplete_review_is_not_read_as_one(tmp_path):
    quoted = (f"Quoting <!-- claude-review reviewed-sha={HEAD} depth=full trigger=other --> "
              "and <!-- claude-review paused --> from the workflow.\n")
    body = ("## Claude review: incomplete\n\n" + quoted
            + "\n<!-- claude-review incomplete trigger=other -->\n")
    outputs = decide(tmp_path, [own(body)])
    assert outputs["mode"] == "full"
    assert outputs["from"] == BASE


def test_a_paused_marker_quoted_mid_review_is_not_a_pause_notice(tmp_path):
    reviews = [own(f"Round {n}.\n\n<!-- claude-review reviewed-sha={HEAD[:-1]}{n} "
                   f"depth=delta trigger=push -->\n") for n in range(2)]
    quoting = own("## Claude review: incomplete\n\nIt says <!-- claude-review paused --> here.\n"
                  "\n<!-- claude-review incomplete trigger=push -->\n")
    # The bound is reached; with no pause notice posted yet, the run posts one.
    assert decide(tmp_path, [*reviews, quoting], push=True, limit=3)["mode"] == "paused"


def test_push_triggered_incomplete_reviews_count_towards_the_bound(tmp_path):
    incomplete = own("## Claude review: incomplete\n\nRead half.\n"
                     "\n<!-- claude-review incomplete trigger=push -->\n")
    assert decide(tmp_path, [incomplete] * 2, push=True, limit=3)["mode"] == "full"
    assert decide(tmp_path, [incomplete] * 3, push=True, limit=3)["mode"] == "paused"
    # Asked for by comment, they do not count.
    asked = own("## Claude review: incomplete\n\nRead half.\n"
                "\n<!-- claude-review incomplete trigger=other -->\n")
    assert decide(tmp_path, [asked] * 3, push=True, limit=3)["mode"] == "full"


def test_an_incomplete_review_ends_with_its_own_marker(stubs):
    _, comment = post(stubs, {"verdict": "approve", "complete": False, "review": "Half."})
    assert comment.rstrip().endswith("<!-- claude-review incomplete trigger=other -->")
