# github-workflows

Shared GitHub workflows and actions for the L337-org organisation.  Each repository calls them
pinned to a full commit SHA, which its own action-pins check requires, and Dependabot proposes
the bumps.

| Path | What it is | Call it as |
|---|---|---|
| `actions/repo-hygiene` | Composite action: the conventions every repository shares - no internal references, every CI job bounded, the instruction layer intact, the detail layer routed. | a step, after `actions/checkout` |
| `actions/action-pins` | Composite action: every `uses:` names a 40-hex commit SHA. | a step, after `actions/checkout` |
| `.github/workflows/claude-review.yaml` | Reusable workflow: a code review by Claude, full on the first round and a delta of the new commits after that. | a job with `uses:` |

## Using them

```yaml
jobs:
  repo-hygiene:
    runs-on: [ubuntu-latest]
    name: Repository hygiene
    timeout-minutes: 5
    steps:
      - uses: actions/checkout@<sha> # vX.Y.Z
      - uses: L337-org/github-workflows/actions/repo-hygiene@<sha>

  action-pins:
    runs-on: [ubuntu-latest]
    name: Action pins are immutable
    timeout-minutes: 5
    steps:
      - uses: actions/checkout@<sha> # vX.Y.Z
      - uses: L337-org/github-workflows/actions/action-pins@<sha>
```

The review needs a caller with its triggers and a `CLAUDE_CODE_OAUTH_TOKEN` secret; see the
inputs at the top of `.github/workflows/claude-review.yaml`.  A job that calls a reusable
workflow cannot declare `timeout-minutes`; the called workflow's jobs carry the bound, and the
hygiene check reports such a call as not checked rather than failing it.

## Running the hygiene check locally

```bash
uv run --script actions/repo-hygiene/check-repo-hygiene.py /path/to/repository
```

Exit status is 0 clean, 1 on findings, 2 when the scan could not be trusted.

## Changing anything here

Everything here runs in other repositories' CI, so a change reaches them only when each one
bumps its pin.  This repository's own CI runs the action-pins action from the commit under
review and lints every workflow and action with actionlint.  The hygiene check does not run on
this repository: it expects the instruction layer and detail layer of a product repository,
which this one does not have.
