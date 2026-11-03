<h1 align="center">cronward</h1>

<p align="center">Runs your GitHub Actions workflows on schedule from a Linux machine you own, and tells you whether they passed.</p>

<p align="center"><img src="docs/demo/demo.svg" alt="A terminal: cronward add puts me/widget's check.yml on a daily schedule, cronward run nightly --wait starts it and says it passed, and cronward status shows the result and the next slot."></p>

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
fine-grained token per account, limited to Actions in the repos you pick.

## Why not a crontab line with curl

You can: `curl -X POST .../dispatches` from cron, or a hosted cron service
(cron-job.org, a Cloudflare Worker, EventBridge) calling the same URL. That
starts the run. It doesn't tell you how the run went, and it fails quietly:

- **Did it run?** A refused start, an expired token or a turned-off workflow
  is a line in a log nobody reads. cronward follows each run to its end, and
  a check that didn't run is told as didn't run, never as nothing.
- **Twice, or not at all.** A request that times out may still have reached
  GitHub. Sending it again can start the run twice; not sending it can lose
  it. cronward saves its claim on a slot before starting it, and after an
  unclear answer looks for the run instead of sending another.
- **The machine was off.** cron skips those slots without a word. cronward
  runs the latest one once when it is back, and reports the others as
  missed.
- **Where the token lives.** A hosted service holds a token that can start
  your workflows. cronward keeps it on your machine (encrypted from
  systemd 256), and talks only to `api.github.com`.
- **Someone has to look.** cronward gives one notice when a job's result
  changes, and a viewer on your laptop tells you when the server itself
  stopped.

If a late or missing run is fine, keep GitHub's `on: schedule`. If you only
need the start and will look at the Actions tab yourself, the curl line is
enough.

## Install

Needs Linux with systemd and Python 3.11 or newer. Nothing else. Tried on
Arch, Debian 12 and Ubuntu 24.04. Ubuntu 22.04 has Python 3.10, too old
(its `python3.11` package is a release candidate). On RHEL 9 and its kin,
`dnf install python3.11` and run cronward with it: `python3.11
~/.local/bin/cronward install` makes the timer use it too.

> **Tokens are only encrypted with systemd 256 or newer**: Debian 13,
> Ubuntu 24.10, Fedora 41, Arch, or later. On older systems, such as
> Ubuntu 24.04 LTS and Debian 12, cronward works, but keeps each token as a
> plain file only you can read, and anything that can read your files can
> use it. `cronward token set` and `cronward doctor` warn about it. Check
> with `systemctl --version`.

```sh
git clone https://github.com/Frestina/cronward ~/Projects/cronward
~/Projects/cronward/install.sh   # links cronward into ~/.local/bin, and the agent skill
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
`systemd-creds` for your user on this machine. Before systemd 256 it is a
plain file only you can read instead, and `token set` warns you. It never
goes in the config.

```sh
cronward token set personal
```

### 2. Jobs

Each workflow needs `workflow_dispatch:` under `on:`, on its default
branch. Then add it:

```sh
cronward add nightly-check --repo me/weather-widget --workflow check.yml \
  --schedule "0 6 * * *"     # copy the cron line from the workflow
```

`add` checks with GitHub that a token reaches the repo and that the workflow
is there, then writes the job into the config. The time zone is UTC, as on
GitHub, unless you pass `--timezone Europe/Oslo` or set it in the config;
`cronward add --help` has the other options. You can also write a
`[jobs.<name>]` table into the config yourself: every setting is explained
in the file `cronward init` wrote. `cronward remove nightly-check` takes a
job out again.

`cronward workflows personal` lists the repos and workflows the token can
reach.

Once cronward runs a workflow, remove its `schedule:` lines, or the days
GitHub does fire give you a second run.

### 3. Check, then start the timer

```sh
cronward doctor     # config, tokens, repos, workflows, timer
cronward run nightly-check --wait   # start one now, and wait to see it pass
cronward install    # the timer: every 5 minutes, at :01, :06, ...
```

User timers stop when you log out, unless lingering is on
(`loginctl enable-linger`) or a session stays logged in. `cronward install`
tells you which.

### Updating

```sh
cd ~/Projects/cronward && git pull --ff-only && ./install.sh
cronward install    # again, so the timer gets any change to its units
cronward doctor     # its first line says which cronward this is
```

Releases are tagged `vX.Y.Z` with notes on what changed, and `cronward
--version` says which one you run. On a viewer, `cronward install --viewer
HOST` instead.

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

GitHub turns off scheduled workflows in public repos after 60 days without
activity. Once you take out a workflow's `schedule:` lines, as above, that
rule no longer applies to it. If it does happen, while the lines are still
there, cronward turns the workflow back on and tells you (`reenable =
false` to only be told). A workflow you turned off yourself is never turned
on.

## With an agent

The repo has an Agent Skill, [skills/cronward](skills/cronward/SKILL.md).
In Claude Code, install it as a plugin:

```
/plugin install cronward --marketplace Frestina/cronward
```

For other agents, `install.sh` links it into `~/.agents/skills`, and into
`~/.claude/skills` unless the plugin is installed. The plugin is only the
skill: the `cronward` command still comes from the install above, on the
machine that runs it, and on a laptop the skill reaches that machine over
ssh. Then you can ask the agent working in a repo to
"put check.yml on cronward, daily at 06:00". It adds `workflow_dispatch:`,
runs `cronward add`, tests the job with `cronward run --wait`, and takes out
the workflow's `schedule:` once a run has passed.

An agent never handles your token. When the token can't reach the repo yet,
it tells you where to add the repo, and waits. For an agent that does not
read skills, tell it to read `skills/cronward/SKILL.md`.

## Being told

You get a notice when a job's result changes: to failed or didn't run, or
back to passed. A job that keeps failing is one notice, not one an hour.

- A desktop notification on the machine, critical by default so it stays.
- `notify.command`: any program, given the notice as JSON on stdin, such as
  a script that posts to your phone.

GitHub also emails the token's account about each failed run cronward
started, as for any run you start, unless you turned that off (GitHub's
Settings, Notifications, Actions). Those come for every failure;
cronward's notice comes when the result changes, and also for didn't run,
such as a missed slot or a refused start, which GitHub cannot tell you
about.

### On the machines you sit at

If cronward runs on a server you don't look at, install a viewer on your
laptop: the same `git clone` and `install.sh`, then

```sh
cronward install --viewer homeserver   # anything you would type after ssh
```

`ssh homeserver` must work without a prompt: the host already known, and a
key file without a passphrase, as the timer runs ssh in batch mode and
never uses your ssh agent.

Every hour it reads the server's status over one ssh call and gives the
same notices, plus:

- **stopped ticking**: the server's last tick is over 30 minutes old;
- **can't be reached**: for 30 minutes;

and again when that is over. The viewer needs no config and no token.

## Commands

```
cronward init                        write the example config
cronward token set|remove ACCOUNT    store a token (read from stdin), or delete it
cronward workflows ACCOUNT           the repos and workflows the token can reach
cronward add JOB --repo R --workflow W --schedule CRON
                                     check a job with GitHub, then add it to the config
cronward remove JOB                  take a job out of the config
cronward doctor                      check config, tokens, repos, workflows and timer
cronward status [--json]             each job's latest result and next slot
cronward run JOB [--wait [TIME]]     start a job now, and with --wait, say how it went
cronward tick [--dry-run]            what the timer runs
cronward watch HOST                  what a viewer's timer runs
cronward install [--viewer HOST]     the user timer, on this machine or a viewer
cronward uninstall                   remove the timers (config, tokens and state stay)
cronward --version                   which cronward this is
```

`status` exits 1 when a job's latest result is not passed, and `run --wait`
when the run did not pass, for scripts.

## Files

| | |
| --- | --- |
| `~/.config/cronward/config.toml` | the config |
| `~/.local/share/cronward/tokens/` | the tokens, encrypted from systemd 256 |
| `~/.local/state/cronward/state.json` | claims, run IDs, the last 50 results of each job |
| `~/.local/state/cronward/status.json` | what `status --json` shows, written every tick |

The `XDG_*_HOME` variables move them. [SPEC.md](SPEC.md) has every rule.

## Security

- **What the token can do**: start and read workflow runs, and turn
  workflows on and off, in the repos you picked. It cannot read your code,
  push, or see other repos.
- **Where the token is kept**: encrypted with `systemd-creds`, so only
  your user on this machine can decrypt it. Before systemd 256 (Ubuntu
  24.04, Debian 12) it is a plain file only you can read: a backup of your
  home, or any program running as you, has it. Keep the token narrow and
  give it an expiry date.
- **What cronward does with it**: starts runs, reads them, and turns on a
  workflow GitHub turned off for inactivity (unless `reenable = false`).
- **Where it talks**: only `https://api.github.com`, fixed in the code; a
  redirect to another host is refused. Your `notify.command` and the
  viewer's ssh are the only other traffic, and both are yours to set up.
- **What runs on your machine**: cronward itself. Not a runner, so a pull
  request to a public repo can never run code here.

To report a security problem, see [SECURITY.md](SECURITY.md).

## License

MIT
