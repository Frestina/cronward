"""The cronward command.

    cronward init                        write the example config
    cronward edit                        open the config in your editor, then check it
    cronward token set|remove ACCOUNT    store a token (read from stdin), or delete it;
                                         adds or takes out [accounts.ACCOUNT]
    cronward workflows [ACCOUNT] [--json]
                                         the repos and workflows the tokens can reach
    cronward add JOB --repo R --workflow W --schedule CRON
                                         check a job with GitHub, then add it to the config
    cronward remove JOB                  take a job out of the config
    cronward doctor                      check config, tokens, repos, workflows and timer
    cronward status [--json]             each job's latest result and next slot
    cronward status --host HOST [--json] the same, as a viewer last fetched it from HOST
    cronward run JOB [--wait [TIME]]     start a job now, and with --wait, say how it went
    cronward tick [--dry-run]            what the timer runs
    cronward watch HOST                  what a viewer's timer runs
    cronward install [--viewer HOST]     the user timer, on the main machine or a viewer
    cronward uninstall                   remove the timers
    cronward --on HOST COMMAND [ARGS]    run a cronward command on HOST, the main
                                         machine, over your ssh
    cronward --version                   which cronward this is

Exit codes: 0 fine, 1 a job's latest result is not passed, or with --host the
main machine cannot be reached or has stopped ticking (status), the start
did not go through or with --wait the run did not pass (run), GitHub's check
refused the job (add), a check failed (doctor) or the main machine was not
reached (watch), 2 an error. With --on, the far side's exit code, or 2 when
ssh failed.
"""

import argparse
import getpass
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
import traceback

from cronward import (__version__, config, cron, doctor, notify, remote, state, status, tick,
                      timers, tokens)
from cronward.config import ConfigError
from cronward.github import Client, GitHubError, HTTPProblem

EXIT_OK, EXIT_NOT_PASSED, EXIT_ERROR = 0, 1, 2
TOKEN = re.compile(r"[A-Za-z0-9_]+")  # github_pat_..., ghp_...


def parser() -> argparse.ArgumentParser:
    out = argparse.ArgumentParser(
        prog="cronward",
        description="Starts GitHub Actions workflows on a schedule from this machine, and "
                    "says whether they passed.")
    out.add_argument("--version", action="version", version=f"cronward {__version__}")
    out.add_argument("--on", metavar="HOST",
                     help="run the command on HOST, the main machine, over your ssh; comes "
                          "first")
    sub = out.add_subparsers(dest="cmd", metavar="COMMAND", required=True)
    sub.add_parser("init", help="write the example config")
    sub.add_parser("edit", help="open the config in your editor, then check it")
    p = sub.add_parser("token", help="store or delete an account's token")
    p.add_argument("action", choices=("set", "remove"))
    p.add_argument("account")
    p = sub.add_parser("workflows", help="the repos and workflows the tokens can reach")
    p.add_argument("account", nargs="?", help="default: every account in the config")
    p.add_argument("--json", action="store_true", help="exact data, for scripts")
    p = sub.add_parser("add", help="check a job with GitHub, then add it to the config")
    p.add_argument("job", help="the job's name, used by commands and in notices")
    p.add_argument("--repo", required=True, metavar="OWNER/NAME")
    p.add_argument("--workflow", required=True, metavar="FILE",
                   help="its file name under .github/workflows, or its ID")
    p.add_argument("--schedule", required=True, metavar="CRON",
                   help='5 fields, as in GitHub\'s schedule, such as "0 6 * * *"')
    p.add_argument("--account", help="default: the account whose token reaches the repo")
    p.add_argument("--ref", help="branch or tag to run; default: the repo's default branch")
    p.add_argument("--timezone", help='for the schedule; default: the config\'s, "UTC" unless '
                                      "set")
    p.add_argument("--finish-within", metavar="TIME",
                   help='a run not finished by then didn\'t run, such as "2h"')
    p.add_argument("--input", action="append", default=[], metavar="KEY=VALUE",
                   help="an input for the workflow; give it again for more")
    p = sub.add_parser("remove", help="take a job out of the config")
    p.add_argument("job")
    sub.add_parser("doctor", help="check config, tokens, repos, workflows and timer")
    p = sub.add_parser("status", help="each job's latest result and next slot")
    p.add_argument("--json", action="store_true", help="exact data, for scripts")
    p.add_argument("--host", help="on a viewer: the status it last fetched from HOST, the "
                                  "main machine")
    p = sub.add_parser("run", help="start a job now, outside its schedule")
    p.add_argument("job")
    p.add_argument("--wait", nargs="?", const="", metavar="TIME",
                   help="follow the run to its end, at most TIME (default: the job's "
                        "finish_within), and exit 0 only if it passed")
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
          "Next: store a token for each account with `cronward token set <account>`, "
          "add jobs with `cronward add` (or in the file), check with `cronward doctor`, "
          "then `cronward install`.")
    return EXIT_OK


def cmd_token(action: str, account: str) -> int:
    if not config.NAME.fullmatch(account):
        print(f"cronward: {account!r} is not an account name", file=sys.stderr)
        return EXIT_ERROR
    if action == "remove":
        print(f"removed the token for {account}" if tokens.remove(account)
              else f"no token stored for {account}")
        if state.path().exists():
            # Its expiry would otherwise warn about a token that is gone.
            with state.locked(wait=True):
                data = state.load()
                if data["accounts"].pop(account, None) is not None:
                    state.save(data)
        try:
            said = _remove_account(account)
        except ConfigError as e:
            print(f"cronward: the token is removed, but [accounts.{account}] is not taken out of "
                  f"the config: {e}", file=sys.stderr)
            return EXIT_ERROR
        if said:
            print(said)
        return EXIT_OK
    try:
        cfg = config.load(required=False) if config.config_path().exists() else None
    except ConfigError:
        cfg = None
    if cfg is not None and account not in cfg.accounts:
        print(f"note: the config has no [accounts.{account}] yet; it is added once GitHub "
              "accepts the token")
    if sys.stdin.isatty():
        token = getpass.getpass(f"Fine-grained token for {account} (not shown): ")
    else:
        token = sys.stdin.readline()
    token = token.strip()
    if not token:
        print("cronward: no token given; nothing stored", file=sys.stderr)
        return EXIT_ERROR
    if not TOKEN.fullmatch(token):
        # Not shown: it may be most of a real token.
        print("cronward: that is not a token: it has spaces, quotes or other characters a "
              "GitHub token never has; nothing stored", file=sys.stderr)
        return EXIT_ERROR
    client = Client(token)
    try:
        login = notify.clean(client.user().get("login", "?"))
    except HTTPProblem as e:
        print(f"cronward: GitHub refused the token ({e.status}: {notify.clean(e.message)}); "
              "nothing stored", file=sys.stderr)
        return EXIT_ERROR
    except GitHubError as e:
        print(f"cronward: cannot check the token: {notify.clean(e)}; nothing stored",
              file=sys.stderr)
        return EXIT_ERROR
    how, why = tokens.store(account, token)
    if state.path().exists():
        # The old token's expiry would otherwise warn until a tick reaches GitHub.
        with state.locked(wait=True):
            data = state.load()
            data["accounts"][account] = {"expires": client.expires}
            state.save(data)
    print(f"stored the token of GitHub user {login} for {account}, {how}")
    print(f"expires: {client.expires or 'never'}")
    if why:
        print(tokens.warning(account, notify.clean(why)), file=sys.stderr)
    if token.startswith("ghp_") or token.startswith("gho_"):
        print("note: this is not a fine-grained token; one limited to Actions: read and write "
              "on the repos of your jobs is safer")
    try:
        added = _add_account(account)
    except ConfigError as e:
        print(f"cronward: the token is stored, but [accounts.{account}] is not in the config: "
              f"{e}", file=sys.stderr)
        return EXIT_ERROR
    if added:
        print(added)
    return EXIT_OK


def _add_account(account: str) -> str | None:
    """Adds [accounts.<account>] to the config if it lacks it; says what it did."""
    path = config.config_path()
    with config.editing():
        try:
            text = path.read_text()
        except FileNotFoundError:
            return (f"note: there is no config at {path}, so no [accounts.{account}] in it; "
                    "write one with: cronward init")
        new = config.with_account(text, account)
        if new == text:
            return None
        config.save(new)
    return f"added [accounts.{account}] to {path}"


def _remove_account(account: str) -> str | None:
    """Takes [accounts.<account>] out of the config unless a job uses it;
    says what it did."""
    path = config.config_path()
    with config.editing():
        try:
            text = path.read_text()
        except FileNotFoundError:
            return None
        cfg = config.parse(text, str(path))
        if account not in cfg.accounts:
            return None
        users = [j.name for j in cfg.jobs if j.account == account]
        if users:
            jobs = (f"job {users[0]} uses it" if len(users) == 1
                    else f"jobs {', '.join(users)} use it")
            return (f"note: kept [accounts.{account}] in {path}, as {jobs}. Without a token "
                    f"{'it does' if len(users) == 1 else 'they do'} not run: store one with "
                    f"`cronward token set {account}`, or take "
                    + (f"it out with `cronward remove {users[0]}`" if len(users) == 1
                       else "each out with `cronward remove JOB`"))
        config.save(config.without_account(text, account))
    return f"removed [accounts.{account}] from {path}"


def _workflows(account: str, jobs: tuple[config.Job, ...]) -> dict:
    """What the account's token reaches: {"account", "error", "repos": [{"repo",
    "error", "workflows": [{"file", "id", "name", "state", "jobs"}]}]}, with
    the names of the jobs already on each workflow."""
    out: dict = {"account": account, "error": None, "repos": []}
    try:
        client = tick.default_client(account)
        repos = client.repos()
    except (tokens.TokenError, GitHubError) as e:
        out["error"] = notify.clean(e)
        return out
    for repo in repos:
        name = notify.clean(repo.get("full_name", "?"))
        entry: dict = {"repo": name, "error": None, "workflows": []}
        out["repos"].append(entry)
        try:
            found = client.workflows(name)
        except HTTPProblem as e:
            entry["error"] = (f"cannot list workflows ({e.status}: the token may lack Actions "
                              "access here)")
            continue
        except GitHubError as e:
            entry["error"] = notify.clean(e)
            continue
        for w in found:
            file = notify.clean(str(w.get("path", "?")).rsplit("/", 1)[-1])
            ids = {file, str(w.get("id"))}
            entry["workflows"].append({
                "file": file, "id": w.get("id"), "name": notify.clean(w.get("name", "")),
                "state": notify.clean(w.get("state", "?")),
                "jobs": [j.name for j in jobs
                         if j.repo.lower() == name.lower() and j.workflow in ids]})
    return out


def cmd_workflows(account: str | None, as_json: bool) -> int:
    if account is not None and not config.NAME.fullmatch(account):
        print(f"cronward: {account!r} is not an account name", file=sys.stderr)
        return EXIT_ERROR
    cfg = config.load(required=account is None)
    names = (account,) if account else cfg.accounts
    if not names:
        raise ConfigError(f"no accounts in {config.config_path()}: store a token with: "
                          "cronward token set <name>, which adds [accounts.<name>]")
    found = [_workflows(name, cfg.jobs) for name in names]
    if as_json:
        json.dump({"accounts": found}, sys.stdout, indent=1, sort_keys=True)
        print()
    for acct in [] if as_json else found:
        head = f"{acct['account']}: " if len(found) > 1 else ""
        if acct["error"]:
            print(f"{head}{acct['error']}", file=sys.stderr)
        elif not acct["repos"]:
            print(f"{head}the token for {acct['account']} reaches no repos")
        for repo in acct["repos"]:
            if repo["error"] or not repo["workflows"]:
                print(f"{head}{repo['repo']}: {repo['error'] or 'no workflows'}")
            for w in repo["workflows"]:
                jobs = "".join(f", job {j}" for j in w["jobs"])
                print(f"{head}{repo['repo']}  {w['file']}  {w['name']}  ({w['state']}{jobs})")
    return EXIT_ERROR if any(a["error"] for a in found) else EXIT_OK


def cmd_edit() -> int:
    path = config.config_path()
    if not path.exists():
        raise ConfigError(f"no config at {path}; write one with: cronward init")
    found = ("nano", "nvim", "vim", "vi")
    editor = (os.environ.get("VISUAL") or os.environ.get("EDITOR")
              or next((e for e in found if shutil.which(e)), None))
    if not editor:
        raise ConfigError(f"no editor: set $EDITOR (none of {', '.join(found)} is on the "
                          f"PATH), or edit {path} yourself")
    with config.editing():
        try:
            subprocess.run([*shlex.split(editor), str(path)])
        except OSError as e:
            raise ConfigError(f"cannot run {editor}: {e.strerror}") from None
    try:
        cfg = config.load()
    except ConfigError as e:
        print(f"cronward: the config does not read now: {e}\nTicks stop with this error "
              "until it is fixed: cronward edit", file=sys.stderr)
        return EXIT_ERROR
    print(f"{path} reads: {len(cfg.accounts)} accounts, {len(cfg.jobs)} jobs. "
          "Check them with: cronward doctor")
    return EXIT_OK


def cmd_status(as_json: bool, host: str | None) -> int:
    if host is None:
        st = status.build(config.load(), state.load())
        now = None
    else:
        try:
            st = notify.copy(host)
        except notify.NoCopy as e:
            print(f"cronward: {e}", file=sys.stderr)
            return EXIT_ERROR
        now = time.time()
    if as_json:
        json.dump(st, sys.stdout, indent=1, sort_keys=True)
        print()
    else:
        print(notify.clean(status.text(st, now)))
    return EXIT_OK if status.all_passed(st) else EXIT_NOT_PASSED


TOKENS_PAGE = "https://github.com/settings/personal-access-tokens"


class CheckFailed(Exception):
    """GitHub's answers say the job would not run; says why."""


def _account_for(accounts: tuple[str, ...], repo: str) -> tuple[str, Client]:
    """The one account of these whose token reaches repo. ConfigError when
    several do, CheckFailed when none does."""
    reached, refused = [], []
    for account in accounts:
        try:
            client = tick.default_client(account)
            client.repo(repo)
        except tokens.TokenError as e:
            refused.append(f"{account}: {e}")
        except HTTPProblem as e:
            refused.append(f"{account}: " + ("the token cannot see the repo" if e.status == 404
                                             else str(e)))
        else:
            reached.append((account, client))
    if len(reached) > 1:
        raise ConfigError(f"the tokens of {', '.join(a for a, _ in reached)} all reach {repo}; "
                          "say which with --account")
    if not reached:
        raise CheckFailed("\n".join([
            f"no account's token reaches {repo}:", *(f"  {r}" for r in refused),
            f"Add {repo} to the token's repository access at {TOKENS_PAGE} (Actions: read and "
            "write), or store a token with: cronward token set <account>. Then run this again.",
        ]))
    return reached[0]


def _check(client: Client, account: str, repo: str, workflow: str, reenable: bool) -> str:
    """The workflow's state, when cronward can start it; CheckFailed if not."""
    try:
        wf = client.workflow(repo, workflow)
    except HTTPProblem as e:
        if e.status == 403 and not e.rate_limited:
            raise CheckFailed(f"the token for {account} cannot read the workflows of {repo} "
                              f"({notify.clean(e.message)}). Give it Actions: read and write "
                              f"on {repo} at {TOKENS_PAGE}, then run this again.") from None
        if e.status != 404:
            raise CheckFailed(str(e)) from None
        try:
            files = [str(w.get("path", "")).rsplit("/", 1)[-1] for w in client.workflows(repo)]
        except GitHubError:
            files = []
        raise CheckFailed(f"{workflow} is not a workflow on {repo}'s default branch, and a new "
                          "workflow file must be there before it can be started"
                          + (f". There are: {', '.join(files)}" if files else "")) from None
    wf_state = wf.get("state")
    if wf_state == "disabled_manually":
        raise CheckFailed("the workflow is turned off in GitHub; turn it on there first")
    if wf_state == "disabled_inactivity" and not reenable:
        raise CheckFailed("GitHub turned the workflow off for inactivity, and reenable is off; "
                          "turn it on in GitHub first")
    if wf_state not in ("active", "disabled_inactivity"):
        raise CheckFailed(f"GitHub says the workflow is {wf_state}")
    return wf_state


def cmd_add(a: argparse.Namespace) -> int:
    with config.editing():
        return _add(a)


def _add(a: argparse.Namespace) -> int:
    path = config.config_path()
    try:
        text = path.read_text()
    except FileNotFoundError:
        raise ConfigError(f"no config at {path}; write one with: cronward init") from None
    cfg = config.parse(text, str(path))
    if any(j.name == a.job for j in cfg.jobs):
        raise ConfigError(f"there is a job {a.job} already; take it out first with: "
                          f"cronward remove {a.job}")
    if not cfg.accounts:
        raise ConfigError(f"no accounts in {path}: store a token with: "
                          "cronward token set <name>, which adds [accounts.<name>]")
    if a.account is not None and a.account not in cfg.accounts:
        raise ConfigError(f"no account {a.account!r} in {path}; there are: "
                          f"{', '.join(cfg.accounts)}. A new one is added by storing its "
                          f"token: cronward token set {a.account}")
    settings: dict[str, object] = {"repo": a.repo, "workflow": a.workflow,
                                   "schedule": a.schedule}
    for key, value in (("ref", a.ref), ("timezone", a.timezone),
                       ("finish_within", a.finish_within)):
        if value is not None:
            settings[key] = value
    inputs = {}
    for item in a.input:
        key, eq, value = item.partition("=")
        if not eq or not key:
            raise ConfigError(f"--input {item!r} is not KEY=VALUE")
        inputs[key] = value
    if inputs:
        settings["inputs"] = inputs
    # Read back once before GitHub is asked, so a wrong value is said first.
    config.with_job(text, a.job, {"account": a.account or cfg.accounts[0], **settings})
    try:
        account, client = _account_for((a.account,) if a.account else cfg.accounts, a.repo)
        wf_state = _check(client, account, a.repo, a.workflow, cfg.reenable)
    except CheckFailed as e:
        print(f"cronward: {notify.clean(e)}\nNothing was added.", file=sys.stderr)
        return EXIT_NOT_PASSED
    except GitHubError as e:
        print(f"cronward: cannot check with GitHub: {notify.clean(e)}\nNothing was added.",
              file=sys.stderr)
        return EXIT_ERROR
    new = config.with_job(text, a.job, {"account": account, **settings})
    config.save(new)
    job = config.parse(new).job(a.job)
    print(f"added {job.name} to {path}: {job.repo} {job.workflow}, {job.schedule.text} "
          f"{job.zone_name}, account {account}")
    if wf_state == "disabled_inactivity":
        print("note: GitHub turned the workflow off for inactivity; the first start turns it "
              "back on")
    print(f"next slot: {status.when(cron.next_slot(job.schedule, job.zone, time.time()))}")
    print(f"try it now with: cronward run {job.name} --wait")
    return EXIT_OK


def cmd_remove(name: str) -> int:
    path = config.config_path()
    with config.editing():
        try:
            text = path.read_text()
        except FileNotFoundError:
            raise ConfigError(f"no config at {path}") from None
        config.parse(text, str(path)).job(name)
        config.save(config.without_job(text, name))
    print(f"removed {name} from {path}")
    print("If GitHub should run the workflow on its own again, put its `schedule:` back.")
    return EXIT_OK


def cmd_run(name: str, wait: str | None) -> int:
    cfg = config.load()
    job = cfg.job(name)
    limit = config.duration(wait, "--wait") if wait else job.finish_within
    now = time.time()
    notify.send(tick.run(cfg, manual=job, now=now), cfg)
    rec = next((r for r in state.load()["results"]
                if r["job"] == job.name and r["slot"] == now), None)
    if rec and rec["kind"] == state.PENDING:
        print(f"started {job.name}: "
              f"{notify.clean(rec.get('url') or 'looking for the run on GitHub')}")
        if wait is None:
            print("the next tick follows it to its end; see `cronward status`")
            return EXIT_OK
        print(f"waiting up to {tick.span(limit)} for it to end")
        rec, notices = tick.wait(cfg, job, now, now + limit)
        notify.send(notices, cfg)
        if rec and rec["kind"] == state.PENDING:
            print(f"{job.name} is still running after {tick.span(limit)}; the tick follows it "
                  "to its end, see `cronward status`")
            return EXIT_NOT_PASSED
    if rec is None:
        return EXIT_NOT_PASSED
    line = f"{job.name} {state.WORDS[rec['kind']]}" + (f": {rec['reason']}" if rec["reason"]
                                                        else "")
    print(notify.clean(line + (f"\n{rec['url']}" if rec.get("url") else "")))
    return EXIT_OK if rec["kind"] == state.PASSED else EXIT_NOT_PASSED


def cmd_tick(dry: bool) -> int:
    try:
        cfg = config.load()
    except ConfigError as e:
        cfg = config.Config()
        notify.send(tick.failed(cfg, str(e)), cfg)
        raise
    if dry:
        for line in tick.dry_run(cfg) or ["no jobs in the config; nothing is due"]:
            print(line)
        return EXIT_OK
    try:
        notices = tick.run(cfg)
    except state.Busy as e:
        print(f"{e}; this tick does nothing")
        return EXIT_OK
    except state.StateError as e:
        notify.send(tick.failed(cfg, str(e)), cfg)
        raise
    except Exception as e:
        notify.send(tick.failed(cfg, f"{type(e).__name__}: {e}"), cfg)
        raise
    notify.send(notices, cfg)
    return EXIT_OK


def commands() -> tuple[str, ...]:
    """cronward's own command names, the only ones --on runs."""
    sub = next(a for a in parser()._actions if isinstance(a, argparse._SubParsersAction))
    return tuple(sub.choices)


def _then_status(args: argparse.Namespace) -> int:
    """A command that changes the config or a token, then status.json to match."""
    if args.cmd == "edit":
        rc = cmd_edit()
    elif args.cmd == "token":
        rc = cmd_token(args.action, args.account)
    elif args.cmd == "add":
        rc = cmd_add(args)
    else:
        rc = cmd_remove(args.job)
    status.refresh()
    return rc


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    try:
        on = remote.split(argv)
        if on:
            return remote.run(*on, commands())
    except remote.RemoteError as e:
        print(f"cronward: {e}", file=sys.stderr)
        return EXIT_ERROR
    args = parser().parse_args(argv)
    if args.on is not None:
        print("cronward: --on comes first: cronward --on HOST COMMAND", file=sys.stderr)
        return EXIT_ERROR
    try:
        if args.cmd == "init":
            return cmd_init()
        if args.cmd in ("edit", "token", "add", "remove"):
            return _then_status(args)
        if args.cmd == "workflows":
            return cmd_workflows(args.account, args.json)
        if args.cmd == "doctor":
            return doctor.run()
        if args.cmd == "status":
            return cmd_status(args.json, args.host)
        if args.cmd == "run":
            return cmd_run(args.job, args.wait)
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
    except Exception:
        # Uncaught, Python exits 1, which the units count as a result, not a failure.
        traceback.print_exc()
        return EXIT_ERROR
    raise AssertionError(args.cmd)
