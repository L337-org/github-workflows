"""Behaviour of the action-pins check: which `uses:` references it accepts and which it fails.

The step's script is read out of `actions/action-pins/action.yml` and run under bash over a
scratch directory of workflow files, so what is tested is the script that ships.
"""

import pathlib
import subprocess

import pytest
import yaml

ROOT = pathlib.Path(__file__).resolve().parent.parent
SHA = "3d3c42e5aac5ba805825da76410c181273ba90b1"


def check(tmp_path, uses):
    (tmp_path / "workflows").mkdir()
    (tmp_path / "workflows" / "ci.yaml").write_text(f"jobs:\n  a:\n    steps:\n      - uses: {uses}\n")
    action = yaml.safe_load((ROOT / "actions" / "action-pins" / "action.yml").read_text())
    (step,) = action["runs"]["steps"]
    return subprocess.run(
        ["bash", "-c", step["run"]], cwd=tmp_path, env={"DIRECTORIES": "workflows", "PATH": "/usr/bin:/bin"},
        capture_output=True, text=True, timeout=30,
    )


@pytest.mark.parametrize("uses", [
    f"actions/checkout@{SHA}",
    f"actions/checkout@{SHA} # v7.0.1",
    f"'actions/checkout@{SHA}'",
    "./.github/workflows/slack-on-failure.yaml",
    "L337-org/github-workflows/.github/workflows/slack-on-pull-request.yaml@v2",
    "L337-org/github-workflows/actions/repo-hygiene@v2",
    "L337-org/github-workflows/actions/repo-hygiene@v2.0.0",
    "'L337-org/github-workflows/actions/action-pins@v2'",
])
def test_accepted(tmp_path, uses):
    result = check(tmp_path, uses)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("uses", [
    "actions/checkout@v7",
    "actions/checkout@main",
    f"actions/checkout@{SHA[:12]}",
    "L337-org/github-workflows/actions/repo-hygiene@main",
    "L337-org/github-workflows/actions/repo-hygiene@v2-rc",
    "L337-org/github-workflows/actions/repo-hygiene@vnext",
    "L337-org/other-repo/actions/thing@v2",
    "someone-else/github-workflows/actions/repo-hygiene@v2",
    "evil/L337-org/github-workflows/actions/repo-hygiene@v2",
])
def test_refused(tmp_path, uses):
    result = check(tmp_path, uses)
    assert result.returncode == 1
    assert "pinned to a full 40-hex commit SHA" in result.stderr
