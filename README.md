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

A job that calls a reusable workflow cannot declare `timeout-minutes`; the called workflow's
jobs carry the bound.  The hygiene check cannot see those jobs from the calling repository, so it
reports such a call as not checked rather than failing or passing it.

## The Claude review

The caller decides when a review runs and who may ask for one; this workflow decides how it
runs.  It needs a `CLAUDE_CODE_OAUTH_TOKEN` secret, a subscription token from
`claude setup-token`, and every review draws on the allowance of whoever generated it.  Its
inputs are at the top of `.github/workflows/claude-review.yaml`.

- **Depth.**  A pull request with no earlier review from this workflow gets a full review of
  everything it changes, by default with `opus` at high effort.  One that has been reviewed gets a
  delta review of the commits since, by default with `sonnet` at medium effort.  A delta review
  reads the surrounding code and the whole pull request's diff, but not the earlier review.  Neither has a turn cap; the job's 60-minute timeout is the only bound.
- **Where the last review stopped.**  Each review comment ends with a marker naming the commit it
  reviewed.  Only this workflow's own comments are read for it.  A force-push that drops that
  commit from the branch falls back to a full review, and a request for a commit already
  reviewed is answered without running one.
- **What Claude can do.**  Read the checkout and run `git diff`, `git log` and `git show`.  It
  returns a verdict, approve or changes requested, and a review body as structured output, and a
  later step posts them, so the session never holds a token that can write to GitHub.
- **One at a time.**  Runs for the same pull request queue on the job, so only real requests
  reach the queue.  A running review is never cancelled.  Of two waiting, the newer one runs,
  and the range it covers starts at the last commit a posted review recorded, so commits the
  older one would have reviewed are not skipped.  A burst of pushes during a review produces one
  delta review of all of them.
- **The caller needs** `contents: read` and `pull-requests: write`, and must pass the secret
  explicitly.

## Running the hygiene check locally

From the root of the repository to check, fetch the script at the commit that repository's
`repo-hygiene` step pins, so a local run checks what its CI checks, and run it there:

```bash
sha=<the commit after @ in the repository's repo-hygiene step>
gh api -H 'Accept: application/vnd.github.raw' \
  "repos/L337-org/github-workflows/contents/actions/repo-hygiene/check-repo-hygiene.py?ref=$sha" \
  > /tmp/check-repo-hygiene.py
uv run --script /tmp/check-repo-hygiene.py .
```

From a clone of this repository, against any checkout:

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
