"""The cronward command.

    cronward init                        write the example config
    cronward token set|remove ACCOUNT    store a token (read from stdin), or delete it
    cronward workflows ACCOUNT           the repos and workflows the token can reach
    cronward doctor                      check config, tokens, repos, workflows and timer
    cronward status [--json]             each job's latest result and next slot
    cronward run JOB                     start a job now
    cronward tick [--dry-run]            what the timer runs
    cronward watch HOST                  what a viewer's timer runs
    cronward install [--viewer HOST]     the user timer, on the main machine or a viewer
    cronward uninstall                   remove the timers

Exit codes: 0 fine, 1 a job's latest result is not passed (status), a check
failed (doctor) or the main machine was not reached (watch), 2 an error.
"""

import argparse
import getpass
import json
import sys

from cronward import config, doctor, notify, state, status, tick, timers, tokens
from cronward.config import ConfigError
from cronward.github import Client, GitHubError, HTTPProblem

EXIT_OK, EXIT_NOT_PASSED, EXIT_ERROR = 0, 1, 2


def parser() -> argparse.ArgumentParser:
    out = argparse.ArgumentParser(
        prog="cronward",
        description="Starts GitHub Actions workflows on a schedule from this machine, and "
                    "says whether they passed.")
    sub = out.add_subparsers(dest="cmd", metavar="COMMAND", required=True)
    sub.add_parser("init", help="write the example config")
    p = sub.add_parser("token", help="store or delete an account's token")
    p.add_argument("action", choices=("set", "remove"))
    p.add_argument("account")
    p = sub.add_parser("workflows", help="the repos and workflows a token can reach")
    p.add_argument("account")
    sub.add_parser("doctor", help="check config, tokens, repos, workflows and timer")
    p = sub.add_parser("status", help="each job's latest result and next slot")
    p.add_argument("--json", action="store_true", help="exact data, for scripts")
    p = sub.add_parser("run", help="start a job now, outside its schedule")
    p.add_argument("job")
    p = sub.add_parser("tick", help="what the timer runs every 5 minutes")
    p.add_argument("--dry-run", action="store_true",
                   help="say what is due, and change nothing")
    p = sub.add_parser("watch", help="what a viewer's timer runs")
    p.add_argument("host", help="the main machine, as you would type it after ssh")
    p = sub.add_parser("install", help="install the user timer")
    p.add_argument("--viewer", metavar="HOST",
                   help="on a machine you sit at: notify about cronward on HOST")
    sub.add_parser("uninstall", help="remove the timers (config, tokens and state stay)")
    return out


def cmd_init() -> int:
    path = config.config_path()
    if path.exists():
        print(f"{path} is there already; nothing written")
        return EXIT_OK
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(config.TEMPLATE)
    print(f"wrote {path}\n"
          "Next: add your jobs there, store a token for each account with "
          "`cronward token set <account>`, check with `cronward doctor`, then "
          "`cronward install`.")
    return EXIT_OK


def cmd_token(action: str, account: str) -> int:
    if not config.NAME.fullmatch(account):
        print(f"cronward: {account!r} is not an account name", file=sys.stderr)
        return EXIT_ERROR
    if action == "remove":
        print(f"removed the token for {account}" if tokens.remove(account)
              else f"no token stored for {account}")
        return EXIT_OK
    try:
        cfg = config.load(required=False)
    except ConfigError:
        cfg = None
    if cfg is not None and account not in cfg.accounts:
        print(f"note: the config has no [accounts.{account}] yet")
    if sys.stdin.isatty():
        token = getpass.getpass(f"Fine-grained token for {account} (not shown): ")
    else:
        token = sys.stdin.readline()
    token = token.strip()
    if not token:
        print("cronward: no token given; nothing stored", file=sys.stderr)
        return EXIT_ERROR
    client = Client(token)
    try:
        login = client.user().get("login", "?")
    except HTTPProblem as e:
        print(f"cronward: GitHub refused the token ({e.status}: {e.message}); nothing stored",
              file=sys.stderr)
        return EXIT_ERROR
    except GitHubError as e:
        print(f"cronward: cannot check the token: {e}; nothing stored", file=sys.stderr)
        return EXIT_ERROR
    how = tokens.store(account, token)
    print(f"stored the token of GitHub user {login} for {account}, {how}")
    print(f"expires: {client.expires or 'never'}")
    if token.startswith("ghp_") or token.startswith("gho_"):
        print("note: this is not a fine-grained token; one limited to Actions: read and write "
              "on the repos of your jobs is safer")
    return EXIT_OK


def cmd_workflows(account: str) -> int:
    try:
        client = tick.default_client(account)
        repos = client.repos()
    except (tokens.TokenError, GitHubError) as e:
        print(f"cronward: {e}", file=sys.stderr)
        return EXIT_ERROR
    if not repos:
        print(f"the token for {account} reaches no repos")
    for repo in repos:
        name = repo.get("full_name", "?")
        try:
            found = client.workflows(name)
        except HTTPProblem as e:
            print(f"{name}: cannot list workflows ({e.status}: the token may lack Actions "
                  "access here)")
            continue
        except GitHubError as e:
            print(f"{name}: {e}")
            continue
        if not found:
            print(f"{name}: no workflows")
        for w in found:
            file = str(w.get("path", "?")).rsplit("/", 1)[-1]
            print(f"{name}  {file}  {notify.clean(w.get('name', ''))}  "
                  f"({w.get('state', '?')})")
    return EXIT_OK


def cmd_status(as_json: bool) -> int:
    cfg = config.load()
    st = status.build(cfg, state.load())
    if as_json:
        json.dump(st, sys.stdout, indent=1, sort_keys=True)
        print()
    else:
        print(notify.clean(status.text(st)))
    return EXIT_OK if status.all_passed(st) else EXIT_NOT_PASSED


def cmd_run(name: str) -> int:
    cfg = config.load()
    job = cfg.job(name)
    notify.send(tick.run(cfg, manual=job), cfg)
    rec = state.latest(state.load(), job.name, final=False)
    if rec and rec["kind"] == state.PENDING:
        print(f"started {job.name}: {rec.get('url') or 'looking for the run on GitHub'}")
        print("the next tick follows it to its end; see `cronward status`")
        return EXIT_OK
    if rec:
        print(f"{job.name} {state.WORDS[rec['kind']]}: {notify.clean(rec['reason'])}")
    return EXIT_NOT_PASSED


def cmd_tick(dry: bool) -> int:
    try:
        cfg = config.load()
    except ConfigError as e:
        tick.failed(config.Config(), str(e))
        raise
    if dry:
        for line in tick.dry_run(cfg):
            print(line)
        return EXIT_OK
    try:
        notices = tick.run(cfg)
    except state.Busy as e:
        print(f"{e}; this tick does nothing")
        return EXIT_OK
    except state.StateError as e:
        tick.failed(cfg, str(e))
        raise
    except Exception as e:
        tick.failed(cfg, f"{type(e).__name__}: {e}")
        raise
    notify.send(notices, cfg)
    return EXIT_OK


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        if args.cmd == "init":
            return cmd_init()
        if args.cmd == "token":
            return cmd_token(args.action, args.account)
        if args.cmd == "workflows":
            return cmd_workflows(args.account)
        if args.cmd == "doctor":
            return doctor.run()
        if args.cmd == "status":
            return cmd_status(args.json)
        if args.cmd == "run":
            return cmd_run(args.job)
        if args.cmd == "tick":
            return cmd_tick(args.dry_run)
        if args.cmd == "watch":
            return notify.watch(args.host, config.load(required=False))
        if args.cmd == "install":
            return timers.install(args.viewer)
        if args.cmd == "uninstall":
            return timers.uninstall()
    except (ConfigError, state.StateError, state.Busy, timers.TimersError) as e:
        print(f"cronward: {e}", file=sys.stderr)
        return EXIT_ERROR
    except KeyboardInterrupt:
        return EXIT_ERROR
    raise AssertionError(args.cmd)
