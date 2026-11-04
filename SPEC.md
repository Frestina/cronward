# cronward spec

This is the source of truth for cronward. Change this file when a decision
changes.

## The problem

GitHub Actions can run a workflow on a schedule (`on: schedule` with a cron
line), but GitHub's scheduler is unreliable. Runs come late, sometimes by
hours, or not at all, and some repos never get a scheduled run. Starting the
same workflow by hand (`workflow_dispatch`) works every time. GitHub's own
documentation says scheduled runs may be delayed or dropped under load.

## What cronward is

A timer on an always-on Linux machine that starts chosen workflows in chosen
repos on a schedule, through GitHub's API with `workflow_dispatch`, follows
each run to its end and says whether it passed.

Every result is one of three:

- **passed**: a run cronward started for that slot ended with `success`.
- **failed**: the run ran and ended `failure`, `timed_out` or
  `startup_failure`.
- **didn't run**: everything else. The slot was missed, the start was
  refused, the workflow was off, the run was cancelled or skipped, or it had
  not finished in time. The result says why.

Only `success` is passed. Nothing unknown is ever shown as passed.

### Terms

- **Job**: one workflow in one repo, with a schedule, named in the config.
- **Slot**: one time a job's schedule names, such as 06:00 on Nov 3.
- **Tick**: one run of `cronward tick`, every 5 minutes from a user timer.
- **Main machine**: the always-on machine that ticks and keeps the state.
- **Viewer**: a machine the author sits at, which reads the main machine's
  status over ssh and notifies.

## What must never happen

1. **A run that didn't happen looks like a pass.** Only a run cronward
   started for that slot, ended `success`, is passed.
2. **A slot runs twice.** The claim of a slot is saved before its run is
   started. A tick that dies after that never starts the slot again: the
   next tick looks for a run GitHub may have started for it, and otherwise
   reports it as didn't run.
3. **Code from a repo runs on the machine.** cronward starts runs on
   GitHub's machines. It is not a runner and runs nothing from a repo.
4. **cronward talks to a host other than api.github.com.** The address is
   fixed in the code and a redirect elsewhere is refused. The only other
   traffic is what the user configures: `notify.command`, and a viewer's
   ssh to the main machine (`watch`, and `--on`).
5. **A token is written into the config or a log.**

## Config

`~/.config/cronward/config.toml` (`$XDG_CONFIG_HOME`). `cronward init`
writes a commented example. Unknown keys are errors, so a typo is caught.

`cronward add` adds a job, and `cronward remove` takes one out. Both change
the file's text, so its comments stay, write through a symlink, and read the
result back before saving it. They hold a lock from reading the file to
saving it, so two at once (two agents, say) never lose one's change. `add`
first asks GitHub whether an account's token reaches the repo and whether
the workflow is there and can be started, and says what to give the token
when it cannot read the workflows. Without `--account` it takes the one
account whose token reaches the repo. When none does, it says where to give
the token access, and adds nothing.

```toml
timezone = "UTC"        # default. Or "local", or an IANA name like "Europe/Oslo"
finish_within = "6h"    # default for every job: a run not finished by then didn't run
reenable = true         # turn a workflow GitHub disabled for inactivity back on

[accounts.personal]     # a name for one token; holds no settings yet
[accounts.work]

[jobs.nightly-check]    # the job's name, used by commands and in status
account  = "personal"
repo     = "me/weather-widget"
workflow = "check.yml"  # the file name under .github/workflows, or the workflow's ID
schedule = "0 6 * * *"  # 5-field cron, as in GitHub's schedule
# ref = "main"          # default: the repo's default branch
# inputs = { level = "full" }
# timezone = "Europe/Oslo"
# finish_within = "2h"
# enabled = false

[notify]
desktop = true          # notify-send on this machine
urgency = "critical"    # low, normal or critical
command = ""            # optional: run with each notice as JSON on stdin
```

- **Accounts.** A fine-grained token belongs to one resource owner (a user
  or an organisation), so each owner whose repos have jobs needs an account.
- **Schedule.** The cron syntax GitHub uses: numbers, `*`, `,`, `-` and `/`,
  plus month and day names. Day 0 and 7 are Sunday. When both day of month
  and day of week are set, either one matching is enough, as in cron.
  Schedules that fire more than once in 5 minutes are refused, because the
  tick runs every 5 minutes.
- **Time zone.** UTC by default, so a line copied from a workflow means the
  same time. When clocks go back, a slot in the repeated hour runs once. When
  they go forward, a slot in the skipped hour runs at the first minute that
  exists after it.

## Tokens

One fine-grained personal access token per account, limited to the repos of
its jobs, with **Actions: read and write**. GitHub adds Metadata: read.

`cronward token set <account>` reads the token from stdin, checks it with
GitHub, and stores it under `~/.local/share/cronward/tokens/`
(`$XDG_DATA_HOME`), never in the config, which people often put in their
dotfiles. With `systemd-creds` (systemd 256 or newer) it is encrypted with
`--user`, bound to this machine and user. Without it, it is a plain file
only the user can read, in a folder only the user can open, never readable
by others even for a moment. That is the case on Ubuntu 24.04 and Debian
12, likely home servers, so it is said loudly: the README says it before
the install, `token set` prints a warning with the reason (such as the
systemd version) and what to do, and `doctor` warns on every run while a
token is a plain file, and says when the machine can now encrypt it.
`token set` refuses text with characters no GitHub token has, without
showing it. A token is never refused for being plain: on those
systems cronward would not work at all.

GitHub sends a token's expiry date with every answer. cronward keeps it and
warns once a day from 14 days before. A refused token makes the job's slot
didn't run, with "token refused (expired or revoked?)".

## A tick

The user timer runs `cronward tick` at minute 1 of every 5 (:01, :06, ...)
and once at start after a missed time (`Persistent=true`). Ticks never
overlap: each holds a lock on the state.

For each enabled job:

1. **Due slots** are the schedule's slots after the job's `through` time and
   up to now. A new job starts from the first tick that sees it; it has no
   past slots. A disabled job's `through` follows the clock, so turning it on
   again reports nothing missed. So does that of a job taken out of the
   config: added again, it has no slots from the time it was out. A run of
   it that was still going ends as didn't run, with no notice.
2. **Missed slots.** When more than one slot is due (the machine was off),
   the latest one runs. The others are recorded as one didn't run result,
   "missed N slots from A to B".
3. **The claim.** `through` moves to now and the slot is recorded as
   pending. The state is saved before anything is started. `through` never
   moves back, so a clock that is set back claims no slot a second time.

Then, for each claimed slot:

4. **The workflow's state.** `active` goes ahead. `disabled_inactivity` is
   turned back on (with `reenable`, the default) and the notice says so.
   `disabled_manually` didn't run: cronward never turns on a workflow the
   user turned off. Anything else didn't run, with GitHub's word for it.
   GitHub's 60-day rule only turns off *scheduled* workflows in public
   repos (its docs, "Disabling and enabling a workflow"), so once the
   `schedule:` lines are out, `disabled_inactivity` should not happen;
   `reenable` covers the time before that.
5. **The start.** `POST .../dispatches` with `return_run_details`, on `ref`
   or the default branch. The answer holds the run's ID and URL.
   - Retried after 5, 10 and 20 seconds only on 408, 429, a rate limit, or a
     connection that never opened.
   - A 5xx or a lost answer can come after GitHub has started the run, so it
     is not retried. cronward looks for a `workflow_dispatch` run of that
     workflow created since the start, and adopts it. It looks again on the
     next ticks for 10 minutes, then gives up: didn't run.
   - Other refusals are didn't run, with what they mean: 401 the token, 403
     the token's permissions, 404 repo or workflow not found or not visible,
     422 GitHub's message (such as no `workflow_dispatch` trigger).

Then, for every pending result:

6. **Following.** `GET .../runs/<id>`. `success` is passed; `failure`,
   `timed_out` and `startup_failure` are failed; any other conclusion
   (`cancelled`, `skipped`, `neutral`, `stale`, `action_required`) is didn't
   run. A run not completed after `finish_within` is didn't run, "not
   finished after 6h". A read that fails keeps it pending.
7. **Status.** The tick writes `~/.local/state/cronward/status.json`, with
   its time, and notifies.

`cronward tick --dry-run` prints what is due and why, and changes nothing.
`cronward run <job>` starts a job now, outside its schedule, recorded as a
manual slot. It waits for a tick that holds the lock to finish, and says it
is waiting. With `--wait [TIME]` it follows the run to its end, for at most
TIME or the job's `finish_within`, gives the notice a tick would, and exits
0 only if the run passed. A run still going at the limit is left to the
ticks.

The state (`~/.local/state/cronward/state.json`) holds each job's `through`
time, the last 50 results of each job, and each account's token expiry. A
state file that does not read stops every tick with an error until it is
fixed or moved away. Moving it away starts every job afresh from that tick.

## Notifications

A notice goes out when a job's latest result changes kind: to failed or
didn't run, or back to passed after one of those. A job that keeps failing
is one notice, not one per run; `status` shows the rest. The first passed
result of a new job makes no notice.

- **Desktop**: `notify-send`, critical by default, because Omarchy's shell
  hides other notifications after 30 seconds. A machine with no session
  just has no notice; the tick goes on.
- **Command**: `notify.command` (a string split like a shell would, or a
  list) is run with the notice as JSON on stdin, such as a script that posts
  to ntfy. cronward itself never talks to that host.

A tick that fails (a config or state that does not read, or anything
unexpected) gives one notice for each new error. The next tick that works
resets it, so the same error coming back is told again.

GitHub also emails the token's account when a run it started fails (seen
in a test, with the account's default notification settings), for every
failure. cronward does not depend on that, and GitHub cannot tell of a
missed slot or a refused start.

### Viewers

`cronward install --viewer <host>` installs a user timer on a machine the
author sits at, hourly at :08: soon enough for jobs that run daily, and few
logins on the main machine. It runs `cronward watch <host>`: one ssh call
(batch mode, no agent, no forwarding) that reads the main machine's
status.json with `cat`, so cronward need not be on the main machine's PATH
for ssh. It needs no config and no token.

The viewer keeps its own record of what it has said, and says:

- The same changes the main machine notifies about.
- **The main machine stopped ticking**: its last tick is more than 30
  minutes old, once, and again when ticks come back.
- **The main machine can't be reached**: after 30 minutes, once, and again
  when it is back. When the viewer itself is offline it says nothing.

The minute keeps clear of the hour and the quarter hours, where other
timers crowd, so they don't ssh into the same machine at once.

The viewer keeps what it fetched as `status-<host>.json` in its state
folder (written atomically, the same schema), and records in its own file
when it last fetched, and the last error and since when.
`cronward status --host <host>` shows that copy, and talks to nothing:
with `--json`, it is status.json plus a `viewer` object,
`{"host", "fetched", "error", "offline", "unreachable_since",
"stopped_ticking"}`, so a reader such as a bar widget can tell a fresh copy
from an old one, and this machine offline from the main machine out of
reach. Stopped ticking is judged at the time of the fetch: the main
machine's last tick was more than 30 minutes before it. Text mode says
"as of <time>, from <host>", and when it applies "cannot reach <host> since
<time>" or "<host> has stopped ticking". It exits 1 when a job's latest
result is not passed, the main machine cannot be reached or has stopped
ticking, and 2 when there is no copy yet.

### Commands on the main machine, from a viewer

`cronward --on <host> <command> [args]` runs a cronward command on the
main machine, for a viewer such as a laptop with a bar widget that changes
something there: `cronward --on homeserver add ...`. It is the user's
normal ssh, with their keys and agent, so it needs a login that can run
commands, unlike a viewer that only reads the status. With a terminal
attached it uses `ssh -t`, so `token set` reads the token there and `edit`
opens an editor; without one, batch mode, so it fails rather than waits for
a prompt nobody sees. cronward stays the only thing that does ssh: a widget
calls cronward, never ssh itself.

Only cronward runs on the far side: the command name is checked against
cronward's own commands first, the host may not start with `-`, and the
arguments are quoted for the far side's shell (a POSIX shell, as for
`watch`). It runs `~/.local/bin/cronward`, else `cronward` on the PATH
there, and says so when neither is there. The exit code is passed through;
ssh's own failure (255) is 2. Output is passed through as it comes: the far
side is cronward, which cleans what it prints.

`cronward edit` opens the config in `$VISUAL`, `$EDITOR`, or the first of
nano, nvim, vim and vi that it finds, holding the config lock while it is
open (an `add` meanwhile waits and says so), then checks that the config
reads. Over `--on HOST` the desktop's `$EDITOR` is not set, so the fallback
is what an editor button on a viewer gets.

`cronward workflows [<account>] --json` is what a picker needs:
`{"accounts": [{"account", "error", "repos": [{"repo", "error",
"workflows": [{"file", "id", "name", "state", "jobs"}]}]}]}`, where `jobs`
names the jobs already on that workflow. With no account, every account
in the config. It exits 2 when an account's repos could not be listed.

## Commands

| Command | |
| --- | --- |
| `cronward init` | Write the example config |
| `cronward edit` | Open the config in the user's editor, then check that it reads |
| `cronward token set <account>` | Check and store a token, read from stdin |
| `cronward token remove <account>` | Delete a stored token |
| `cronward workflows [<account>] [--json]` | The repos and workflows the token can reach (`GET /user/repos`, which lists exactly the repos a fine-grained token was given), and which are jobs; with no account, every account's |
| `cronward add <job> --repo R --workflow W --schedule C` | Check a job with GitHub, then add it to the config |
| `cronward remove <job>` | Take a job out of the config |
| `cronward doctor` | Check config, tokens, repos, workflows, timer and lingering |
| `cronward status [--json]` | Each job's latest result and next slot, and the last tick |
| `cronward status --host <host> [--json]` | The same, as a viewer last fetched it from the main machine |
| `cronward run <job> [--wait [TIME]]` | Start a job now, and with `--wait`, say how it went |
| `cronward tick [--dry-run]` | What the timer runs |
| `cronward watch <host>` | What a viewer's timer runs |
| `cronward install [--viewer <host>]` | The user timer, on the main machine or a viewer |
| `cronward uninstall` | Remove the timers; config, tokens and state stay |
| `cronward --on <host> <command>` | Run a cronward command on the main machine, over the user's ssh |
| `cronward --version` | Which cronward this is |

Exit codes: 0 fine, 1 a job's latest result is not passed, or with `--host`
the main machine cannot be reached or has stopped ticking (status), the
start did not go through or with `--wait` the run did not pass (run),
GitHub's check refused the job (add), a check failed (doctor), or the main
machine was not reached (watch), 2 an error. With `--on`, the far side's
code, or 2 when ssh failed.

## Agents

[skills/cronward/SKILL.md](skills/cronward/SKILL.md) tells an agent working
in any repo how to put one of its workflows on cronward: make it startable,
`add` it, test it with `run --wait`, then take out its `schedule:`. It is an
Agent Skill (a folder with a SKILL.md), so agents that read skills find it,
and any other agent can be told to read the file. The repo is also a Claude
Code plugin marketplace (`.claude-plugin/`) with one plugin, `cronward`,
that holds only the skill. `install.sh` links the skill into
`~/.agents/skills`, and into `~/.claude/skills` unless the plugin is
installed, so Claude Code never has it twice.

An agent never handles a token: when one is missing or lacks access, the
skill has it tell the user what to do and stop. Change the skill in the same
commit as the commands it describes.


## Requirements

Python 3.11 or newer, standard library only; with an older one,
`bin/cronward` says so in one line. Linux with systemd user timers, and a
user manager that keeps running: lingering, or an autologin session.
`install` and `doctor` say when neither is there, and when the user manager
cannot be reached at all, and never turn lingering on themselves. CI runs
the tests on Debian 12 and Ubuntu 24.04 with their own Python, and checks
the one line on Ubuntu 22.04.

`doctor` warns when notices from the machine reach no one: no notify-send
(as on a server) or desktop off, and no `notify.command`. That is fine when
a viewer watches the machine, which the machine cannot know. The services run at low priority (Nice, idle IO, a
memory and time limit), so they don't disturb other work on the machine.

## Versions

The version is `__version__` in `cronward/__init__.py`. `--version`, the
first line of `doctor` and the User-Agent sent to GitHub
(`cronward/<version>`) all say it, and the plugin's
`.claude-plugin/plugin.json` carries the same one, which Claude Code uses to
see an update. A release is a tag `vX.Y.Z` on main with
notes on what changed. Before 1.0, the minor number goes up when an update
needs something from the user (a config change, or `cronward install`
again), and the patch number otherwise. A new version reads the config and
state.json an older one wrote.

## Not in v1

Running a check locally, a web UI, GitHub Apps, other forges (Forgejo and
Gitea have a similar API), finding workflows by a marker in the file, and
schedules more often than every 5 minutes.
