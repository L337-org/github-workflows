"""Behaviour of the two shared Slack workflows' posting steps, and the hygiene check over callers.

Each step's script is read out of its workflow file and run under bash with the environment
GitHub would give it, against stand-ins for `curl` and `gh` on PATH.  So what is tested is the
script that ships, not a copy of it.  What this cannot test: the GitHub expressions that fill the
environment, and Slack itself.
"""

import importlib.util
import json
import os
import pathlib
import stat
import subprocess
import textwrap

import pytest
import yaml

ROOT = pathlib.Path(__file__).resolve().parent.parent
WORKFLOWS = ROOT / ".github" / "workflows"
TOKEN = "xoxb-test-" + os.urandom(8).hex()

# Records its arguments, its standard input and the header file it was handed, writes the
# canned answer to --output, and prints the canned status, as curl's --write-out would.
CURL = textwrap.dedent("""\
    #!/usr/bin/env bash
    printf '%s\\n' "$@" > "$STUB_DIR/curl.args"
    cat > "$STUB_DIR/curl.stdin"
    out=""
    while [ $# -gt 0 ]; do
      case "$1" in
        --output) out="$2"; shift ;;
        --header) case "$2" in @*) cat "${2#@}" >> "$STUB_DIR/curl.headers" ;; esac; shift ;;
      esac
      shift
    done
    printf '%s' "$STUB_BODY" > "$out"
    printf '%s' "$STUB_STATUS"
    exit "${STUB_EXIT:-0}"
""")

GH = textwrap.dedent("""\
    #!/usr/bin/env bash
    printf '%s' "${STUB_GH:-}"
""")


def step_script(workflow):
    document = yaml.safe_load((WORKFLOWS / workflow).read_text())
    (job,) = document["jobs"].values()
    (step,) = [s for s in job["steps"] if "run" in s]
    return step["run"]


@pytest.fixture
def stubs(tmp_path):
    for name, body in (("curl", CURL), ("gh", GH)):
        path = tmp_path / name
        path.write_text(body)
        path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return tmp_path


def run(workflow, stubs, env, status="200", body='{"ok": true}', curl_exit=0):
    full = {
        "PATH": f"{stubs}:{os.environ['PATH']}",
        "STUB_DIR": str(stubs),
        "STUB_STATUS": status,
        "STUB_BODY": body,
        "STUB_EXIT": str(curl_exit),
        "TMPDIR": str(stubs),
        **env,
    }
    result = subprocess.run(
        ["bash", "-c", step_script(workflow)], env=full, capture_output=True, text=True,
        timeout=30,
    )
    posted = (stubs / "curl.stdin").exists()
    return result, posted


def pull_request(**overrides):
    env = {
        "SLACK_BOT_TOKEN": TOKEN,
        "CHANNEL": "#l337-org",
        "EVENT_NAME": "pull_request",
        "ACTION": "opened",
        "REPO": "L337-org/quaffed",
        "HEAD_REPO": "L337-org/quaffed",
        "DRAFT": "false",
        "NUMBER": "42",
        "TITLE": "Bump <things> & stuff",
        "AUTHOR": "dependabot[bot]",
        "BASE": "main",
        "PR_URL": "https://github.com/L337-org/quaffed/pull/42",
    }
    env.update(overrides)
    return env


ANNOUNCER = "slack-on-pull-request.yaml"


@pytest.mark.parametrize("action,what", [
    ("opened", "opened"), ("reopened", "reopened"), ("ready_for_review", "ready for review"),
])
def test_an_actionable_pull_request_is_posted(stubs, action, what):
    result, posted = run(ANNOUNCER, stubs, pull_request(ACTION=action))
    assert result.returncode == 0, result.stderr
    assert posted
    payload = json.loads((stubs / "curl.stdin").read_text())
    assert payload["channel"] == "#l337-org"
    assert payload["unfurl_links"] is False and payload["unfurl_media"] is False
    assert f"Pull request {what} in L337-org/quaffed" in payload["text"]
    assert "#42 Bump &lt;things&gt; &amp; stuff" in payload["text"]
    assert "`dependabot[bot]`" in payload["text"]
    assert (stubs / "curl.headers").read_text().startswith(f"Authorization: Bearer {TOKEN}")
    assert "https://slack.com/api/chat.postMessage" in (stubs / "curl.args").read_text()


def test_the_token_is_not_on_curls_command_line(stubs):
    run(ANNOUNCER, stubs, pull_request())
    assert TOKEN not in (stubs / "curl.args").read_text()


@pytest.mark.parametrize("overrides,reason", [
    ({"HEAD_REPO": "stranger/quaffed"}, "comes from stranger/quaffed"),
    ({"HEAD_REPO": ""}, "comes from a deleted fork"),
    ({"DRAFT": "true"}, "is a draft"),
    ({"DRAFT": "true", "ACTION": "reopened"}, "is a draft"),
    ({"ACTION": "synchronize"}, "was synchronize"),
])
def test_a_pull_request_needing_no_action_is_skipped_with_a_notice(stubs, overrides, reason):
    result, posted = run(ANNOUNCER, stubs, pull_request(**overrides))
    assert result.returncode == 0, result.stderr
    assert not posted
    assert "::notice::Not posted" in result.stdout and reason in result.stdout


def test_a_fork_is_skipped_even_without_a_token(stubs):
    result, posted = run(ANNOUNCER, stubs, pull_request(HEAD_REPO="stranger/quaffed", SLACK_BOT_TOKEN=""))
    assert result.returncode == 0 and not posted


@pytest.mark.parametrize("event", ["pull_request_target", "push"])
def test_any_other_trigger_fails(stubs, event):
    result, posted = run(ANNOUNCER, stubs, pull_request(EVENT_NAME=event))
    assert result.returncode == 1 and not posted
    assert f"called from a `{event}` event" in result.stderr


@pytest.mark.parametrize("overrides,message", [
    ({"SLACK_BOT_TOKEN": ""}, "No SLACK_BOT_TOKEN secret"),
    ({"CHANNEL": ""}, "No channel was given"),
])
def test_missing_configuration_fails_naming_it(stubs, overrides, message):
    result, posted = run(ANNOUNCER, stubs, pull_request(**overrides))
    assert result.returncode == 1 and not posted
    assert message in result.stderr


REPORTER = "slack-on-failure.yaml"


def failed_run(**overrides):
    env = {
        "SLACK_BOT_TOKEN": TOKEN,
        "CHANNEL": "#docker-mcp",
        "GH_TOKEN": "unused",
        "STUB_GH": "build (failure)",
        "EVENT_NAME": "workflow_run",
        "REPO": "L337-org/docker-mcp",
        "WORKFLOW": "Check inbound changes",
        "CONCLUSION": "failure",
        "RUN_ID": "1",
        "RUN_NUMBER": "7",
        "RUN_ATTEMPT": "1",
        "RUN_URL": "https://github.com/L337-org/docker-mcp/actions/runs/1",
        "BRANCH": "main",
        "SHA": "0123456789abcdef",
        "TRIGGER": "push",
        "WORKFLOW_ID": "9",
        "DEFAULT_BRANCH": "main",
    }
    env.update(overrides)
    return env


def test_a_failed_run_is_posted_to_the_callers_channel(stubs):
    result, posted = run(REPORTER, stubs, failed_run())
    assert result.returncode == 0, result.stderr
    payload = json.loads((stubs / "curl.stdin").read_text())
    assert payload["channel"] == "#docker-mcp"
    assert "*Check inbound changes*" in payload["text"] and "build (failure)" in payload["text"]
    assert (stubs / "curl.headers").read_text().startswith(f"Authorization: Bearer {TOKEN}")
    assert TOKEN not in (stubs / "curl.args").read_text()


@pytest.mark.parametrize("overrides,message", [
    ({"SLACK_BOT_TOKEN": ""}, "No SLACK_BOT_TOKEN secret"),
    ({"CHANNEL": ""}, "No channel was given"),
])
def test_the_reporter_fails_on_missing_configuration(stubs, overrides, message):
    result, posted = run(REPORTER, stubs, failed_run(**overrides))
    assert result.returncode == 1 and not posted
    assert message in result.stderr


# The posting and refusal handling is the same code in both workflows, each a copy of the other,
# so every case runs against both and a copy that drifts fails here.
BOTH = pytest.mark.parametrize("workflow,environment,channel", [
    (ANNOUNCER, pull_request, "#l337-org"),
    (REPORTER, failed_run, "#docker-mcp"),
])


@BOTH
@pytest.mark.parametrize("error,hint", [
    ("not_in_channel", "Invite the Slack app to {channel}"),
    ("channel_not_found", "Check the channel name"),
    ("invalid_auth", "Replace SLACK_BOT_TOKEN"),
    ("token_revoked", "Replace SLACK_BOT_TOKEN"),
    ("missing_scope", "chat:write"),
    ("something_new", ""),
])
def test_a_refusal_fails_with_slacks_error(stubs, workflow, environment, channel, error, hint):
    result, _ = run(workflow, stubs, environment(), body=json.dumps({"ok": False, "error": error}))
    assert result.returncode == 1
    assert f"HTTP 200, {error}." in result.stderr and hint.format(channel=channel) in result.stderr


@BOTH
def test_a_non_slack_answer_is_quoted_and_marked_truncated(stubs, workflow, environment, channel):
    result, _ = run(workflow, stubs, environment(), status="502", body="<html>" + "x" * 1000)
    assert result.returncode == 1
    assert "HTTP 502, <html>" in result.stderr and "(truncated at 500 bytes)" in result.stderr


@BOTH
def test_a_long_acceptance_is_still_read_as_accepted(stubs, workflow, environment, channel):
    body = json.dumps({"ok": True, "message": {"text": "x" * 5000}})
    result, _ = run(workflow, stubs, environment(), body=body)
    assert result.returncode == 0, result.stderr


@BOTH
def test_curl_failing_outright_fails(stubs, workflow, environment, channel):
    result, _ = run(workflow, stubs, environment(), curl_exit=28)
    assert result.returncode == 1 and "curl could not complete" in result.stderr


@BOTH
def test_the_token_reaches_curl_only_in_a_header_file(stubs, workflow, environment, channel):
    run(workflow, stubs, environment())
    assert (stubs / "curl.headers").read_text().startswith(f"Authorization: Bearer {TOKEN}")
    assert TOKEN not in (stubs / "curl.args").read_text()
    assert json.loads((stubs / "curl.stdin").read_text())["channel"] == channel


def hygiene():
    spec = importlib.util.spec_from_file_location(
        "check_repo_hygiene", ROOT / "actions" / "repo-hygiene" / "check-repo-hygiene.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("uses", [
    "L337-org/github-workflows/.github/workflows/slack-on-pull-request.yaml@" + "0" * 40,
    "L337-org/github-workflows/.github/workflows/slack-on-failure.yaml@" + "0" * 40,
    "./.github/workflows/slack-on-failure.yaml",
])
@pytest.mark.parametrize("given,secrets,found", [
    ({"channel": "#quaffed"}, {"SLACK_BOT_TOKEN": "${{ secrets.SLACK_BOT_TOKEN }}"}, []),
    ({"channel": "#quaffed"}, "inherit", []),
    ({"channel": " "}, {"SLACK_BOT_TOKEN": "x"}, ["without a `with: channel:`"]),
    ({}, {"SLACK_BOT_TOKEN": "x"}, ["without a `with: channel:`"]),
    (None, {"SLACK_BOT_TOKEN": "x"}, ["without a `with: channel:`"]),
    ({"channel": "#quaffed"}, {"SLACK_WEBHOOK": "x"}, ["passes `SLACK_WEBHOOK`"]),
    ({"channel": "#quaffed"}, {"SLACK_WEBHOOK": "x", "SLACK_BOT_TOKEN": "x"}, ["passes `SLACK_WEBHOOK`"]),
    ({"channel": "#quaffed"}, None, ["without passing `SLACK_BOT_TOKEN`"]),
    (None, {"SLACK_WEBHOOK": "x"}, ["without a `with: channel:`", "passes `SLACK_WEBHOOK`"]),
])
def test_hygiene_requires_v2_inputs_on_every_slack_call(uses, given, secrets, found):
    body = {"uses": uses}
    if given is not None:
        body["with"] = given
    if secrets is not None:
        body["secrets"] = secrets
    findings = hygiene().slack_channel_findings([(".github/workflows/x.yaml", {"jobs": {"slack": body}})])
    assert len(findings) == len(found)
    for finding, expected in zip(findings, found):
        assert expected in finding


def test_every_shipped_caller_here_names_a_channel():
    documents = [
        (p.name, yaml.safe_load(p.read_text())) for p in sorted(WORKFLOWS.glob("*.yaml"))
    ]
    assert hygiene().slack_channel_findings(documents) == []
