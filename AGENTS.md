# Working in this repo

cronward starts GitHub Actions workflows on a schedule from a Linux machine,
through `workflow_dispatch`, and says whether each run passed.
[SPEC.md](SPEC.md) is the source of truth: read it before changing
behaviour, and change it in the same commit when a decision changes.

## Layout

- `bin/cronward`: the command; `cronward/cli.py` parses its arguments.
- `cronward/cron.py`: cron lines, and the slots they name in a time zone.
- `cronward/config.py`: file locations and the TOML config.
- `cronward/github.py`: the only code that talks to GitHub.
- `cronward/tokens.py`: tokens, encrypted with systemd-creds.
- `cronward/state.py`: state.json, its lock and the result records.
- `cronward/tick.py`: a tick: claim, start, follow. The core.
- `cronward/notify.py`: when to give a notice, giving it, and the viewer.
- `cronward/status.py`: `status` and status.json.
- `cronward/remote.py`: `--on HOST`, a command on the main machine over ssh.
- `cronward/doctor.py`, `cronward/timers.py`: `doctor`, and the systemd units.
- `skills/cronward/SKILL.md`: the skill for agents in other repos. Keep it in
  step with the commands it uses.
- `docs/demo/make.py`: makes the README's recording from real output against
  the FakeGitHub. A test fails when it is out of date; run it again.
- `tests/`: `./tests/run` runs everything. `tests/harness.py` gives each
  test a throwaway home and a FakeGitHub transport; `tests/bin` holds fakes
  (notify-send, systemctl, loginctl, systemd-creds, ssh) that record calls.

## Rules that are never traded away

From the spec's [What must never happen](SPEC.md#what-must-never-happen):

1. A run that didn't happen never looks like a pass. Only `success` of a run
   cronward started for that slot is passed.
2. A slot never runs twice: the claim is saved before the start, and a start
   is only sent again when GitHub cannot have received it.
3. Nothing from a repo runs on the machine.
4. Only `api.github.com`, fixed in `github.py`. Tests replace the transport,
   never the address.
5. No token in the config, the state, a log or the output.

## Conventions

- Python 3.11+, standard library only.
- Text from GitHub or another machine goes through `notify.clean` before it
  is printed or shown.
- Tests need no network, no ssh and no real home.
- Fictional names in code, tests and docs: `me/widget`, `anvil`,
  `homeserver`, `example.org`.
- Docs: sentence case headings, straight quotes, no em dashes, no emoji.
