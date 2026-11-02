"""`cronward doctor`: checks everything a tick needs, and says what to fix.

It only reads: it starts no run and changes nothing on GitHub. It cannot see
whether a workflow has `workflow_dispatch:`, because that would need the
token to read the repo's files; the first start says so if it is missing.
"""

import time

from cronward import config, cron, state, status, timers, tokens
from cronward.config import ConfigError
from cronward.github import GitHubError, HTTPProblem
from cronward.tick import MakeClient, default_client, explain, when


class Report:
    def __init__(self):
        self.failed = 0

    def ok(self, text: str) -> None:
        print(f"ok    {text}")

    def warn(self, text: str) -> None:
        print(f"warn  {text}")

    def fail(self, text: str) -> None:
        self.failed += 1
        print(f"FAIL  {text}")


def run(make: MakeClient = default_client) -> int:
    r = Report()
    try:
        cfg = config.load()
    except ConfigError as e:
        r.fail(str(e))
        return 1
    r.ok(f"config {config.config_path()}: {len(cfg.jobs)} job{'s' * (len(cfg.jobs) != 1)}, "
         f"{len(cfg.accounts)} account{'s' * (len(cfg.accounts) != 1)}")

    clients = {}
    for name in cfg.accounts:
        kind = tokens.kind(name)
        if kind is None:
            r.fail(f"account {name}: no token; store one with: cronward token set {name}")
            continue
        if kind == "plain":
            r.warn(f"account {name}: the token is a plain file, not encrypted "
                   "(systemd-creds was not there when it was stored)")
        try:
            client = make(name)
            login = client.user().get("login", "?")
        except tokens.TokenError as e:
            r.fail(f"account {name}: {e}")
            continue
        except HTTPProblem as e:
            r.fail(f"account {name}: " + ("GitHub refused the token (expired or revoked?)"
                                          if e.status == 401 else str(e)))
            continue
        except GitHubError as e:
            r.fail(f"account {name}: {e}")
            continue
        clients[name] = client
        t = status.expiry(client.expires)
        if t is not None and t - time.time() < status.SOON:
            r.warn(f"account {name}: the token of GitHub user {login} expires {client.expires}")
        else:
            r.ok(f"account {name}: token of GitHub user {login}, expires "
                 f"{client.expires or 'never'}")

    for job in cfg.jobs:
        client = clients.get(job.account)
        if client is None:
            r.fail(f"job {job.name}: not checked, account {job.account} has no working token")
            continue
        try:
            repo = client.repo(job.repo)
            wf = client.workflow(job.repo, job.workflow)
        except HTTPProblem as e:
            r.fail(f"job {job.name}: {explain(e, job)}")
            continue
        except GitHubError as e:
            r.fail(f"job {job.name}: {e}")
            continue
        ref = job.ref or repo.get("default_branch")
        nxt = cron.next_slot(job.schedule, job.zone, time.time())
        where = f"job {job.name}: {job.repo} {wf.get('path', job.workflow)} on {ref}"
        st = wf.get("state")
        if st == "active":
            line = f"{where}, next slot {when(nxt) if nxt else 'never'}"
            r.ok(line if job.enabled else f"{line} (disabled in the config)")
        elif st == "disabled_inactivity":
            (r.warn if cfg.reenable else r.fail)(
                f"{where}: GitHub turned it off for inactivity; "
                + ("the next start turns it back on" if cfg.reenable else "reenable is off"))
        else:
            r.fail(f"{where}: GitHub says the workflow is {st}")

    tick_state = timers.active(timers.TICK.name)
    if tick_state is None:
        r.warn("the timer is not installed on this machine; install it with: cronward install")
    elif tick_state != "active":
        r.fail(f"the timer is {tick_state}; reinstall it with: cronward install")
    else:
        r.ok("the timer is active")
        on = timers.linger()
        if on is False:
            r.warn("lingering is off: the timer only runs while you are logged in (an "
                   "autologin session counts). Turn it on with: loginctl enable-linger")
        try:
            data = state.load()
        except state.StateError as e:
            r.fail(str(e))
        else:
            tick = data.get("last_tick")
            if data.get("tick_error"):
                r.fail(f"the last tick failed: {data['tick_error']}")
            elif tick is None or time.time() - tick > status.STALE:
                r.warn(f"the last tick was {when(tick) if tick else 'never'}")
            else:
                r.ok(f"the last tick was {status.age(time.time() - tick)}")
    return 1 if r.failed else 0
