---
name: cronward
description: Put a GitHub Actions workflow on cronward, which starts it on a schedule from the user's own Linux machine through workflow_dispatch and says whether each run passed; or test, check or remove such a job. Use when asked to schedule a workflow with cronward, to make a workflow's schedule reliable because GitHub's `on: schedule` runs late or not at all, or whether a cronward job passed.
---

# cronward

GitHub's `on: schedule` runs late, or not at all. cronward is a timer on the
user's always-on Linux machine that starts chosen workflows on a schedule
through the API (`workflow_dispatch`), follows each run to its end and says
whether it passed. The runs still happen on GitHub. Each job is one workflow
in one repo with one cron line, kept in the config on that machine.

These steps work for any agent: they are shell commands and file edits.

## Rules

- **Never handle a token.** Don't ask for one, read one, print one or store
  one. The user stores tokens themselves, in their own terminal, with
  `cronward token set <account>`. When a token is missing or lacks access,
  say what to do and stop.
- **Use `cronward add` and `cronward remove`** rather than editing the
  config by hand. They check the job with GitHub first, and keep the file's
  comments.
- **A test run is a real run.** It runs the workflow on GitHub, with
  whatever it does: deploys, releases, messages. Ask before starting a
  workflow that has effects like that.
- Don't install cronward, its timers or lingering unless the user asks.

## Find the machine cronward runs on

Jobs live on the main machine, which holds the config and the tokens. Run
`add`, `remove`, `run`, `status` and `doctor` there.

1. If `systemctl --user cat cronward-tick.timer` succeeds, this is the main
   machine: run `cronward` here (`~/.local/bin/cronward` if it is not on
   `PATH`).
2. Otherwise, if `systemctl --user cat cronward-watch.service` succeeds, this
   machine is a viewer, and its `ExecStart=` line ends with `watch <host>`.
   Run each command there by putting `--on <host>` first:
   `cronward --on <host> status`. It runs cronward over the user's ssh and
   passes the exit code through; 2 with "ssh failed" means ssh could not
   log in (no key, a passphrase). Then ask the user to run the commands, or
   to make ssh to `<host>` work for you.
3. Otherwise cronward is not set up for this user. Tell them, point them to
   the install steps in cronward's README, and stop.

## Put a workflow on cronward

1. **Pick the workflow and the schedule.**
   - The repo is `owner/name`, from `git remote get-url origin`.
   - The workflow is its file name under `.github/workflows/`, such as
     `check.yml`.
   - The schedule is a 5-field cron line: the `cron:` under `on: schedule:`
     if the workflow has one, or what the user asked for. It may fire at
     most every 5 minutes.
   - GitHub's cron lines are UTC, and so is cronward by default. If the user
     gives a time in their own zone, pass `--timezone` with an IANA name
     such as `Europe/Oslo`.
   - One job has one cron line. For a workflow with several, add one job
     per line.
2. **Make the workflow startable.** Its `on:` needs `workflow_dispatch:`.
   Add it beside the other triggers, turning a short form such as
   `on: push` or `on: [push]` into a mapping. Leave `schedule:` in place for
   now.
   - If `workflow_dispatch` has required inputs without a default, plan to
     give each with `--input KEY=VALUE`.
   - The change must be on the default branch (or the branch given with
     `--ref`) before cronward can start it: GitHub only knows workflow files
     on the default branch. Commit and merge it the way this repo does that,
     and ask before pushing if you are unsure.
3. **Add the job.**

   ```sh
   cronward add <job> --repo <owner/name> --workflow <file> --schedule "<cron>"
   ```

   Name the job after the repo and what it does, such as `widget-nightly`:
   letters, digits, `.`, `_` and `-`. Other options: `--account`, `--ref`,
   `--timezone`, `--finish-within` (such as `2h`), `--input KEY=VALUE`.

   | Result | What to do |
   | --- | --- |
   | Exit 0 | Added. It prints the next slot. |
   | Exit 1, "no account's token reaches" | Stop. Ask the user to add the repo to their token's repository access at the page it prints (or to store a token), then run `add` again. You cannot do this for them. |
   | Exit 1, "cannot read the workflows" | Stop. The token reaches the repo but lacks Actions: read and write. Ask the user to give it that at the page it prints, then run `add` again. |
   | Exit 1, "not a workflow on ... default branch" | Step 2 is not merged yet. |
   | Exit 1, "turned off in GitHub" | The user turned the workflow off. Ask before anyone turns it on. |
   | Exit 2 | A wrong value, or several accounts reach the repo (then pass `--account`). The message says which. |

4. **Test it**, after asking if the workflow has effects (see Rules):

   ```sh
   cronward run <job> --wait 8m
   ```

   Keep the time under your own command time limit. It prints the run's URL
   and exits 0 only if the run passed. On exit 1 it prints why:
   - "still running": the timer follows it. Look again later with
     `cronward status --json`.
   - "add `workflow_dispatch:`": step 2 is not on the branch it ran.
   - "may not do this": the token lacks Actions: read and write. The user
     changes the token's permissions on GitHub.
   - failed: the workflow itself failed. That is the repo's to fix, not
     cronward's.
5. **Take out `schedule:`** once a run has passed, or GitHub's own
   scheduled runs come on top of cronward's on the days they do fire. Keep
   `workflow_dispatch:`. Commit it the same way as step 2.
6. **Tell the user** the job's name, its schedule and zone, the next slot,
   and the test run's URL. They get a notice when a job's result changes to
   failed or didn't run, and when it passes again.

## Other tasks

- **Check jobs:** `cronward status --json`. For each of `jobs[]`: `name`,
  `latest` (the newest final result: `kind` is `passed`, `failed` or
  `didnt_run`, with `reason` and `url`), `running` (a run still going),
  and `next` (a Unix time). `last_tick` and `tick_error` say whether the
  timer works. `cronward doctor` checks everything and marks problems
  `FAIL`. On a viewer, `cronward status --host <host> --json` shows the
  status it last fetched, with no ssh: its `viewer` object says when
  (`fetched`), and whether the main machine could not be reached
  (`unreachable_since`, `error`) or had stopped ticking. `cronward watch
  <host>` fetches it again.
- **Take a job off:** `cronward remove <job>`. Put `schedule:` back in the
  workflow if GitHub should run it on its own again.
- **Change a job:** `cronward remove <job>`, then `cronward add` it again
  with the new values.
- **Pause a job:** that is `enabled = false` in its table in the config.
  Ask the user, or take it off.

## Exit codes

0 fine; 1 a result that is not passed (`run --wait`, `status`), a check
GitHub refused (`add`) or a failed check (`doctor`); 2 an error, with the
reason on stderr.
