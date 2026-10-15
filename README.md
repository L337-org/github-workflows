# github-workflows

Shared GitHub workflows and actions for the L337-org organisation.  Each repository calls them
at their major-version tag, such as `@v2`, so a change released here reaches every repository
without a pull request in each.  See [Versions and releases](#versions-and-releases).

| Path | What it is | Call it as |
|---|---|---|
| `actions/repo-hygiene` | Composite action: the conventions every repository shares - no internal references, every CI job bounded, every failed run reported, every Slack post given a channel, the instruction layer intact, the detail layer routed. | a step, after `actions/checkout` |
| `actions/action-pins` | Composite action: every `uses:` names a 40-hex commit SHA, or a version tag of this repository. | a step, after `actions/checkout` |
| `.github/workflows/claude-review.yaml` | Reusable workflow: a code review by Claude, full on the first round and a delta of the new commits after that. | a job with `uses:` |
| `.github/workflows/slack-on-failure.yaml` | Reusable workflow: posts a failed run to Slack, naming the workflow and its failed jobs and linking the run. | a job with `uses:`, in a workflow triggered by `workflow_run` |
| `.github/workflows/slack-on-pull-request.yaml` | Reusable workflow: posts a pull request that needs action to Slack - opened, reopened, or marked ready for review - and nothing else about it. | a job with `uses:`, in a workflow triggered by `pull_request` |

## Using them

```yaml
jobs:
  repo-hygiene:
    runs-on: [ubuntu-latest]
    name: Repository hygiene
    timeout-minutes: 5
    steps:
      - uses: actions/checkout@<sha> # vX.Y.Z
      - uses: L337-org/github-workflows/actions/repo-hygiene@v2

  action-pins:
    runs-on: [ubuntu-latest]
    name: Action pins are immutable
    timeout-minutes: 5
    steps:
      - uses: actions/checkout@<sha> # vX.Y.Z
      - uses: L337-org/github-workflows/actions/action-pins@v2
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
  reads the surrounding code and the whole pull request's diff.  Neither has a turn cap; the
  job's 60-minute timeout is the only bound.
- **Earlier rounds.**  A delta review is given the previous review and every comment posted
  since, and reports under four headings: resolved, still open, declined, and new.  A finding
  someone rejects in a reply is listed as declined and not raised again without new evidence,
  and each review carries its open and declined findings forward, so no round needs the ones
  before it.  A full review after earlier rounds gets the previous review and every reply since
  the first, so a rewrite does not resurrect a declined finding.  The comments are capped at
  60,000 characters, oldest dropped first, and the prompt says when that happened.
- **A bound on automatic rounds.**  The caller says, with `push-triggered`, whether a push
  started the run.  After `max-auto-reviews` push-triggered reviews (default 10) since the last
  full review, a push posts one notice that automatic reviews are paused and then stays quiet.
  A request by comment always runs, and a full review restarts the count.
- **Where the last review stopped.**  Each review comment ends with a marker naming the commit it
  reviewed, the depth, and whether a push started it.  Only this workflow's own comments are
  read for it.  A force-push that drops that commit from the branch falls back to a full review.
  A request by comment for a commit already reviewed is answered without running one; a push of
  one only says so in the log, because nobody asked.
- **Recorded state.**  Every review checks what the change adds to documentation and comments
  for claims about how things are now - counts, measured figures, what was tested, "currently"
  or "until X lands", settings held elsewhere - and raises each, even when true on the day,
  saying where the fact already lives.  Such claims go stale as soon as something changes and are
  then worse than nothing; a record of what was done belongs in the commit message or pull
  request.
- **What Claude can do.**  Read the checkout and run `git diff`, `git log` and `git show`.  It
  returns a verdict, approve or changes requested, and a review body as structured output, and a
  later step posts them, so the session never holds a token that can write to GitHub.
- **One at a time.**  Runs for the same pull request queue on the job, so only real requests
  reach the queue.  A running review is never cancelled.  Of two waiting, the newer one runs,
  and the range it covers starts at the last commit a posted review recorded, so commits the
  older one would have reviewed are not skipped.  A burst of pushes during a review produces one
  delta review of all of them.
- **A request by comment reviews the head it was made against.**  The run starts after the
  comment, so the gate reads when the branch was last pushed from GitHub's activity record,
  which the pusher cannot set, and refuses the request, with a reply saying to ask again, if
  that push is at or after the comment or is not of the current head.  The review then checks
  out the head the gate passed rather than looking it up again.  So nothing pushed after the
  request is reviewed under the token.
- **Bot and fork pull requests are skipped.**  GitHub gives no Actions secrets to a run
  started by Dependabot or by a fork pull request, and bot pull requests are reviewed by hand, so a gate job
  skips those runs with a notice instead of failing.  A pull request a bot opened is skipped
  whoever starts the run, since a person pushing to it or asking by comment brings the token with
  them; a request by comment gets a reply saying so.  A fork pull request is refused even when a
  maintainer asks by comment, because that run carries the token over text written by someone
  with no access; the request gets a reply saying so.  A notice saying no token arrived, on a pull
  request that is neither, means the secret is missing or not passed.
- **A review that returns no verdict says why.**  The comment headed `Claude review did not
  complete` and the run log both carry the session's result record - its status and error text,
  such as a usage limit - without the usage and cost figures.  The comment carries no marker, so
  the next request reviews the same range again.
- **The caller needs** `contents: read` and `pull-requests: write`, and must pass the secret
  explicitly.

## Reporting failed runs to Slack

A failed run is otherwise one red entry in a list nobody opens, so a failure started by a
schedule, a push, a release or a pull request is posted to Slack instead.  Each repository has
one watcher workflow, `report-failures.yaml`, naming the workflows to report; adding such a
workflow means adding its `name:` there.

```yaml
name: 'Report failures'
on:
  workflow_run:
    workflows:
      - 'Check inbound changes'
      - 'Code review'
      - 'Release'
      - 'Nightly fuzzing'
    types: [completed]
permissions:
  actions: read
  contents: read
jobs:
  slack:
    name: Report to Slack
    uses: L337-org/github-workflows/.github/workflows/slack-on-failure.yaml@v2
    with:
      channel: '#l337-org'
      events: '["schedule", "push", "release", "pull_request"]'
    secrets:
      SLACK_BOT_TOKEN: ${{ secrets.SLACK_BOT_TOKEN }}
```

- **What is reported.**  A completed run of a listed workflow started by one of the `events`,
  with any conclusion but success, skipped or neutral, so a cancelled or timed-out run is
  posted as well as a failed one.  A push is reported on the default branch and on a tag; a
  push to another branch has its pusher watching.  A manual run is watched by whoever started
  it, so it does not belong in `events`.  A pull request from a fork is not posted: its run
  uses the fork's own workflow file, so its author writes the job names and branch a post
  would carry, and could put text of their choosing in the channel.  Its failure still shows
  on the pull request.  The post names the repository, the workflow, its trigger, its
  conclusion and the jobs that did not succeed, and links the run.
- **A cancelled run that a newer one replaced is not posted**, as when `cancel-in-progress`
  cancels the first of two merges landing together: the newer run is reported if it fails.
  **Accepted limitation:** a run cancelled by its timeout while a newer run of the same workflow
  was already queued looks the same, so it is not posted either.
- **The hygiene check holds the list.**  It fails on a workflow triggered by `schedule`, `push`,
  `release` or `pull_request` that no watcher reports for that trigger, on one with no `name:`,
  and on a listed name no workflow has, since GitHub matches on the name and a rename leaves the
  list reporting nothing.
- **Where it goes.**  To the caller's `channel`, through the bot token in `SLACK_BOT_TOKEN`
  (see [The Slack token](#the-slack-token)).
- **A post that fails, fails the watcher's run**, with Slack's answer in the error: a missing
  secret or channel, a refusal such as `not_in_channel`, or Slack unreachable after retries.  A transient refusal is
  retried, which can post the same failure twice.  **Accepted limitation:** the watcher's run is
  itself a run nobody is watching, and nothing reports it, so a token that stops working is
  noticed only by the absence of posts.  Filing an issue instead would scatter them across
  repositories if filed in the failing one, and if filed here would need either a credential
  that can write across repositories or a scheduled sweep of every repository's watcher runs.
- **The watcher only fires from the default branch**, where GitHub reads `workflow_run`
  triggers.  So a change to it is tested only once merged.
- **The caller must grant** `actions: read` and `contents: read`, as the example does, or
  GitHub refuses to start the reporter's job and nothing is posted.

## Announcing pull requests to Slack

A pull request nobody has been told about waits until someone happens to open its repository,
which is how a stack of Dependabot pull requests goes unnoticed.  The GitHub app for Slack posts
every later event on a pull request too, so each repository has an announcer instead, posting
only when a pull request needs action:

```yaml
name: 'Announce pull requests'
on:
  pull_request:
    types: [opened, reopened, ready_for_review]
permissions: {}
jobs:
  slack:
    name: Announce to Slack
    uses: L337-org/github-workflows/.github/workflows/slack-on-pull-request.yaml@v2
    with:
      channel: '#l337-org'
    secrets:
      SLACK_BOT_TOKEN: ${{ secrets.SLACK_BOT_TOKEN }}
```

- **What is posted.**  A pull request opened or reopened that is not a draft, and a draft marked
  ready for review, so a draft is announced when it asks for review rather than when it is
  started.  Dependabot's are posted like anyone else's.  The post names the repository, links
  the pull request by number and title, and gives its author and base branch.
- **What is not.**  A pull request from a fork, whose title and author a stranger chooses and
  would reach the channel through the token.  Any other action, such as a push to the branch:
  a caller whose `types:` include one gets a notice asking it to narrow them.  Each skip leaves
  a notice in the run saying why.  A call from any event but `pull_request` fails, including
  `pull_request_target`, which would hand the token to a run over a fork's pull request.
- **Which channel.**  `#docker-mcp`, `#quaffed` and `#send-to-influx` serve their repositories;
  every other repository posts to `#l337-org`.
- **List it in the watcher.**  It runs on `pull_request`, so the hygiene check requires its
  `name:` in `report-failures.yaml` like any other workflow.  A failed announcement is reported
  through the same token, so a token that stops working silences both.

## The Slack token

Both workflows post with `chat.postMessage` as the L337-org Slack app, through its bot token
(`xoxb-`), to the channel each caller names.  One token serves every channel, where an incoming
webhook serves one channel each and would need a secret per channel, twice over.

- **Two organisation secrets, one value.**  `SLACK_BOT_TOKEN` as an Actions secret, and again as
  a Dependabot secret, because a run Dependabot starts sees only Dependabot secrets.  Without the
  second, Dependabot's pull requests are not announced.  An organisation secret on GitHub Free
  reaches public repositories only.
- **The app must be in the channel.**  Invite it to each channel a caller names, or the post
  fails with `not_in_channel`.  A channel is named rather than given by ID, which works for a
  public channel only; a private one needs its ID.
- **What the token can do.**  More than a webhook could: it can post to every channel the app is
  in, and do whatever else the app's scopes allow.  Keep the app's bot scopes to `chat:write`, so
  a leaked token can post and nothing more.  **Accepted limitation:** a leaked token can post to
  any of the app's channels, where a leaked webhook reached one; one secret pair to maintain
  instead of two per channel was judged worth that.
- **Rotating it.**  Reinstall the app or regenerate the token under "OAuth & Permissions" at
  api.slack.com/apps, and replace both secrets.  A refused token fails each post with
  `invalid_auth` or `token_revoked`, naming both secrets.
- **Moving a caller from v1.**  Version 1 takes `SLACK_WEBHOOK` and no `channel`; version 2
  requires `channel` and `SLACK_BOT_TOKEN`.  Move every reference to `@v2` and change the
  callers in the same pull request.  The v2 hygiene check fails a call to either workflow that
  names no channel, so a move that leaves a caller unchanged fails its own pull request rather
  than breaking after merge.

## Running the hygiene check locally

From the root of the repository to check, fetch the script at the version that repository's
`repo-hygiene` step names, so a local run checks what its CI checks, and run it there:

```bash
ref=<what follows @ in the repository's repo-hygiene step, such as v2>
gh api -H 'Accept: application/vnd.github.raw' \
  "repos/L337-org/github-workflows/contents/actions/repo-hygiene/check-repo-hygiene.py?ref=$ref" \
  > /tmp/check-repo-hygiene.py
uv run --script /tmp/check-repo-hygiene.py .
```

From a clone of this repository, against any checkout:

```bash
uv run --script actions/repo-hygiene/check-repo-hygiene.py /path/to/repository
```

Exit status is 0 clean, 1 on findings, 2 when the scan could not be trusted.

## Versions and releases

Callers name a major-version tag, `v2`, which is moved to each release of that major version.
Each release also gets its full tag, such as `v2.0.0`, which never moves.

- **Why a tag and not a commit.**  A commit pin needs a pull request in every repository for
  every change here.  A tag of another repository would normally be refused, because its owner
  can repoint it (DS.6).  Here the `v*` tags are protected by a ruleset that blocks creating,
  moving and deleting them, with a bypass list no wider than `main`'s, so moving `v2` takes the
  same people as merging to `main`.  The action-pins check accepts this repository's version
  tags and nothing else's; third-party actions still need a commit SHA.
- **Releasing.**  Merge to `main`, publish a release with its full tag, then move the major tag
  to the same commit:

  ```bash
  git fetch origin --tags
  git tag -f v2 v2.0.1
  git push -f origin refs/tags/v2
  ```

  Every caller runs the new code from its next run.  Tag only commits that are on `main`: the
  ruleset cannot check that.
- **A breaking change is a new major version.**  Anything that would make an unchanged caller
  stop working or behave differently - a new required input, a removed one, a renamed secret -
  is released as `v3.0.0` with a new `v3` tag, and `v2` stays where it is.  Each repository then
  moves to `v3` in its own pull request, changing its callers at the same time, and nothing
  breaks in between.

## Changing anything here

Everything here runs in other repositories' CI, so a change reaches them as soon as its release
moves the major tag.  This repository's own CI runs the action-pins action from the commit under
review, lints every workflow and action with actionlint, runs the Slack workflows' posting steps
against stand-ins for `curl` and `gh` and the action-pins check over sample references
(`tests/`), and reviews each pull request with the
pull request's own version of the Claude review, so a change to the review reviews itself.  The
hygiene check does not run on this repository: it expects the instruction layer and detail layer
of a product repository, which this one does not have.

To run the tests locally, from the root of a clone:

```bash
PYTHONDONTWRITEBYTECODE=1 uvx --with pyyaml==6.0.3 pytest==8.3.5 -p no:cacheprovider -q tests
```
