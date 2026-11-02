<h1 align="center">cronward</h1>

<p align="center">Runs your GitHub Actions workflows on schedule from a Linux machine you own, and tells you whether they passed.</p>

GitHub's `on: schedule` is unreliable. Runs start late, sometimes by hours,
or never: some repos never get a scheduled run at all, while "Run workflow"
by hand works every time. cronward is the usual workaround done properly. A
systemd timer on an always-on machine starts each workflow through GitHub's
API (`workflow_dispatch`), follows the run to its end, and keeps three
answers apart:

- **passed**: the run cronward started for that slot ended `success`.
- **failed**: it ran and failed.
- **didn't run**: the slot was missed, the start was refused, the workflow
  was turned off, or the run was cancelled or never finished. It says why.

A check that didn't run never looks like it passed.

```
$ cronward status
cronward on homeserver: last tick Nov 3 06:06 (1 min ago)

nightly-check: me/weather-widget check.yml, 0 6 * * * Europe/Oslo
  passed: slot Nov 3 06:00
  https://github.com/me/weather-widget/actions/runs/123456789
  next: Nov 4 06:00

drift-watch: me/dotfiles drift.yml, */15 * * * * UTC
  didn't run: slot Nov 3 06:00, the workflow is turned off in GitHub, and cronward leaves that to you
  next: Nov 3 06:15
```

The runs still happen on GitHub's machines. cronward is not a runner and
never runs code from a repo. It talks only to `api.github.com`, with a
fine-grained token per account that can only start and read workflows.

## Install

Needs Linux with systemd and Python 3.11 or newer. Nothing else.

```sh
git clone https://github.com/Frestina/cronward ~/Projects/cronward
~/Projects/cronward/install.sh   # links cronward into ~/.local/bin
cronward init                     # writes ~/.config/cronward/config.toml
```

### 1. A token for each account

On GitHub: **Settings, Developer settings, Personal access tokens,
Fine-grained tokens, Generate new token**.

- **Resource owner**: the user or organisation that owns the repos. Each
  owner needs its own token, and its own `[accounts.<name>]` in the config.
- **Repository access**: only select repositories, the ones with jobs.
- **Permissions**: Repository permissions, **Actions: Read and write**.
  Nothing else; GitHub adds Metadata: read itself.

Then store it. It is read from stdin, checked with GitHub, and encrypted with
`systemd-creds` for your user on this machine (a file only you can read if
`systemd-creds` is missing). It never goes in the config.

```sh
cronward token set personal
```

### 2. Jobs

Each workflow needs `workflow_dispatch:` under `on:`. Then add it to the
config:

```toml
[jobs.nightly-check]
account = "personal"
repo = "me/weather-widget"
workflow = "check.yml"
schedule = "0 6 * * *"     # copy the cron line from the workflow
# timezone = "Europe/Oslo" # default UTC, as on GitHub
```

`cronward workflows personal` lists the repos and workflows the token can
reach. Every setting is explained in the file `cronward init` wrote.

Once cronward runs a workflow, remove its `schedule:` lines, or the days
GitHub does fire give you a second run.

### 3. Check, then start the timer

```sh
cronward doctor     # config, tokens, repos, workflows, timer
cronward run nightly-check   # try one now
cronward install    # the timer: every 5 minutes, at :01, :06, ...
```

User timers stop when you log out, unless lingering is on
(`loginctl enable-linger`) or a session stays logged in. `cronward install`
tells you which.

## What it does

Every 5 minutes, `cronward tick`:

1. Finds each job's due slots. If the machine was off, the latest missed
   slot runs once when it is back, and the others are reported as missed.
2. Saves its claim on each slot first, so a slot never runs twice.
3. Starts the run and keeps the run's ID. A start is only sent again when
   GitHub cannot have received it. After an unclear answer, cronward looks
   for the run it may have started instead of sending another.
4. Follows open runs to the end. A run not finished after `finish_within`
   (default 6 hours) didn't run.

New jobs start from the next slot, not the past ones. `cronward tick
--dry-run` shows what is due and changes nothing.

### Turned-off workflows

GitHub turns off workflows in public repos after 60 days without activity.
cronward turns such a workflow back on and tells you (`reenable = false` to
only be told). A workflow you turned off yourself is never turned on.

## Being told

You get a notice when a job's result changes: to failed or didn't run, or
back to passed. A job that keeps failing is one notice, not one an hour.

- A desktop notification on the machine, critical by default so it stays.
- `notify.command`: any program, given the notice as JSON on stdin, such as
  a script that posts to your phone.

### On the machines you sit at

If cronward runs on a server you don't look at, install a viewer on your
laptop:

```sh
cronward install --viewer homeserver   # anything you would type after ssh
```

Every 15 minutes it reads the server's status over one ssh call (key files,
batch mode, no agent) and gives the same notices, plus:

- **stopped ticking**: the server's last tick is over 30 minutes old;
- **can't be reached**: for 30 minutes;

and again when that is over. The viewer needs no config and no token.

## Commands

```
cronward init                        write the example config
cronward token set|remove ACCOUNT    store a token (read from stdin), or delete it
cronward workflows ACCOUNT           the repos and workflows the token can reach
cronward doctor                      check config, tokens, repos, workflows and timer
cronward status [--json]             each job's latest result and next slot
cronward run JOB                     start a job now
cronward tick [--dry-run]            what the timer runs
cronward watch HOST                  what a viewer's timer runs
cronward install [--viewer HOST]     the user timer, on this machine or a viewer
cronward uninstall                   remove the timers (config, tokens and state stay)
```

`status` exits 1 when a job's latest result is not passed, for scripts.

## Files

| | |
| --- | --- |
| `~/.config/cronward/config.toml` | the config |
| `~/.local/share/cronward/tokens/` | the tokens, encrypted |
| `~/.local/state/cronward/state.json` | claims, run IDs, the last 50 results of each job |
| `~/.local/state/cronward/status.json` | what `status --json` shows, written every tick |

The `XDG_*_HOME` variables move them. [SPEC.md](SPEC.md) has every rule.

## Security

- **What the token can do**: start and read workflow runs, and turn
  workflows on and off, in the repos you picked. It cannot read your code,
  push, or see other repos.
- **What cronward does with it**: starts runs, reads them, and turns on a
  workflow GitHub turned off for inactivity (unless `reenable = false`).
- **Where it talks**: only `https://api.github.com`, fixed in the code; a
  redirect to another host is refused. Your `notify.command` and the
  viewer's ssh are the only other traffic, and both are yours to set up.
- **What runs on your machine**: cronward itself. Not a runner, so a pull
  request to a public repo can never run code here.

## License

MIT
