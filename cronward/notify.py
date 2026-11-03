"""Notices: when to give one, and giving it.

A job gets a notice when its results change kind, taken in the order they
became final: to failed or didn't run, or back to passed after one of
those. A job's first result gives one only when it is not passed. A
workflow cronward turned back on always gives one, and so does a token that
expires within 14 days, once a day. A tick that fails gives one for each new
error (tick.failed).

A notice goes to notify-send and to notify.command, which gets it as JSON
on stdin. Neither can fail a tick: what goes wrong is printed, and the
tick goes on.

On a viewer, `cronward watch <host>` reads the main machine's status.json
over one ssh call and gives the same notices, from its own record of what
it has said, plus the ones about the main machine itself: not ticking, a
failed tick, or not reachable. See SPEC.md, "Viewers".
"""

import datetime
import html
import json
import re
import shutil
import subprocess
import sys
import time

from cronward import config, state, status
from cronward.config import Config
from cronward.state import DIDNT_RUN, FAILED, PASSED, WORDS

APP = "cronward"
TIMEOUT = 30
UNREACHABLE_FOR = 30 * 60
# Key files only: a timer has no unlocked agent, and a locked one can stall ssh.
SSH = ("ssh", "-T", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
       "-o", "ForwardAgent=no", "-o", "IdentityAgent=none", "-o", "ClearAllForwardings=yes")
REMOTE = 'cat -- "${XDG_STATE_HOME:-$HOME/.local/state}/cronward/status.json"'
OFFLINE = ("Network is unreachable", "Temporary failure in name resolution")
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")


def clean(text: object) -> str:
    """Text from GitHub or another machine, safe to print and show."""
    return _CONTROL.sub("", str(text))


def _result_notice(rec: dict, job: dict | None, prev: str | None) -> dict:
    kind = rec["kind"]
    name = rec["job"]
    title = (f"cronward: {name} passes again" if kind == PASSED
             else f"cronward: {name} {WORDS[kind]}")
    where = f"{job['repo']} {job['workflow']}" if job else name
    slot = (f"started by hand {status.when(rec['slot'])}" if rec.get("manual")
            else f"slot {status.when(rec['slot'])}")
    body = f"{where}, {slot}" + (f": {rec['reason']}" if rec["reason"] else "")
    if rec.get("url"):
        body += f"\n{rec['url']}"
    return {"title": clean(title), "body": clean(body), "job": name, "kind": kind,
            "previous": prev, "repo": job and job["repo"], "workflow": job and job["workflow"],
            "slot": rec["slot"], "reason": clean(rec["reason"]), "url": rec.get("url")}


def changes(notified: dict[str, str | None], finished: list[dict],
            jobs: dict[str, dict]) -> list[dict]:
    """The notices for results that became final, updating notified (job
    name to the kind last told)."""
    out = []
    for rec in sorted(finished, key=lambda r: (r.get("finished") or 0, r["slot"])):
        name = rec["job"]
        if rec.get("note"):
            out.append({**_result_notice(rec, jobs.get(name), notified.get(name)),
                        "title": f"cronward: {name}: workflow turned back on",
                        "body": clean(rec["note"]), "kind": "note"})
        prev = notified.get(name)
        kind = rec["kind"]
        if kind == prev or kind not in (PASSED, FAILED, DIDNT_RUN):
            continue
        if kind != PASSED or prev is not None:
            out.append(_result_notice(rec, jobs.get(name), prev))
        notified[name] = kind
    return out


def decide(cfg: Config, data: dict, finished: list[dict], now: float) -> list[dict]:
    """The main machine's notices after a tick; records them in data."""
    notified = {name: js.get("notified") for name, js in data["jobs"].items()}
    jobs = {j.name: {"repo": j.repo, "workflow": j.workflow} for j in cfg.jobs}
    out = changes(notified, finished, jobs)
    for name, kind in notified.items():
        data["jobs"].setdefault(name, {})["notified"] = kind
    today = datetime.date.fromtimestamp(now).isoformat()
    for name in cfg.accounts:
        acct = data["accounts"].get(name, {})
        t = status.expiry(acct.get("expires"))
        if t is not None and t - now < status.SOON and acct.get("warned") != today:
            acct["warned"] = today
            left = "has expired" if t <= now else f"expires {acct['expires']}"
            out.append({"title": f"cronward: the token for {name} {left}",
                        "body": f"Make a new one and store it with: cronward token set {name}",
                        "kind": "token", "account": name})
    return out


def send(notices: list[dict], cfg: Config) -> None:
    for n in notices:
        print(f"notice: {n['title']}: {n['body']}")
        if cfg.desktop and shutil.which("notify-send"):
            try:
                done = subprocess.run(
                    ["notify-send", f"--app-name={APP}", f"--urgency={cfg.urgency}", "--",
                     n["title"], html.escape(n["body"], quote=False)],
                    stdin=subprocess.DEVNULL, capture_output=True, timeout=TIMEOUT)
                if done.returncode != 0:
                    print(f"notify-send: {done.stderr.decode(errors='replace').strip()}",
                          file=sys.stderr)
            except (OSError, subprocess.TimeoutExpired) as e:
                print(f"notify-send: {e}", file=sys.stderr)
        if cfg.command:
            try:
                done = subprocess.run(list(cfg.command), input=json.dumps(n).encode(),
                                      capture_output=True, timeout=TIMEOUT)
                if done.returncode != 0:
                    print(f"notify command exited {done.returncode}: "
                          f"{done.stderr.decode(errors='replace').strip()}", file=sys.stderr)
            except (OSError, subprocess.TimeoutExpired) as e:
                print(f"notify command: {e}", file=sys.stderr)


class Unreachable(Exception):
    pass


class Offline(Exception):
    pass


def fetch(host: str) -> dict:
    """The main machine's status, over one ssh call."""
    argv = [*SSH, "--", host, REMOTE]
    try:
        done = subprocess.run(argv, capture_output=True, stdin=subprocess.DEVNULL,
                              timeout=TIMEOUT)
    except OSError as e:
        raise Unreachable(f"cannot run ssh: {e.strerror}") from None
    except subprocess.TimeoutExpired:
        raise Unreachable(f"{host} gave no answer in {TIMEOUT}s") from None
    err = done.stderr.decode(errors="replace").strip().splitlines()
    last = clean(err[-1]) if err else f"exit {done.returncode}"
    if done.returncode == 255:
        if any(o in last for o in OFFLINE):
            raise Offline(f"this machine is offline: {last}")
        raise Unreachable(f"cannot reach {host}: {last}")
    if done.returncode != 0:
        if "No such file" in last:
            raise Unreachable(f"{host} has no cronward status: it has never ticked there")
        raise Unreachable(f"cannot read the status on {host}: {last}")
    try:
        data = json.loads(done.stdout)
        if data.get("schema") != status.SCHEMA or not isinstance(data.get("jobs"), list):
            raise ValueError("not a status this version reads")
    except (ValueError, AttributeError) as e:
        raise Unreachable(f"the status on {host} does not read: {e}") from None
    return data


def _viewer_path(host: str):
    safe = re.sub(r"[^A-Za-z0-9_.@-]", "_", host)
    return config.state_dir() / f"viewer-{safe}.json"


def _load_viewer(host: str) -> dict | None:
    try:
        return json.loads(_viewer_path(host).read_text())
    except (OSError, ValueError):
        return None


def watch(host: str, cfg: Config, now: float | None = None) -> int:
    now = time.time() if now is None else now
    v = _load_viewer(host) or {"notified": {}, "seen": {}}
    v.setdefault("notified", {})
    v.setdefault("seen", {})
    notices: list[dict] = []
    try:
        st = fetch(host)
    except Offline as e:
        print(f"{e}; trying again later", file=sys.stderr)
        return 0
    except Unreachable as e:
        since = v.get("unreachable_since") or now
        v["unreachable_since"] = since
        if now - since >= UNREACHABLE_FOR and not v.get("unreachable_said"):
            v["unreachable_said"] = True
            notices.append({"title": f"cronward: cannot reach {host}",
                            "body": f"{e}. Its jobs may not be starting.", "kind": "host"})
        state.write_json(_viewer_path(host), v)
        send(notices, cfg)
        print(e, file=sys.stderr)
        return 1

    if v.pop("unreachable_said", False):
        notices.append({"title": f"cronward: {host} is reachable again", "body": "",
                        "kind": "host"})
    v.pop("unreachable_since", None)
    # Files from before "contacted" have seen jobs; one that only ever
    # failed to reach the host has none.
    first = not v.pop("contacted", bool(v["seen"]))
    v["contacted"] = True

    tick = st.get("last_tick")
    stale = not isinstance(tick, (int, float)) or now - tick > status.STALE
    if stale and not v.get("stale_said"):
        v["stale_said"] = True
        notices.append({"title": f"cronward on {host} has stopped ticking",
                        "body": f"Last tick: {status.when(tick) if tick else 'never'}. Check "
                                f"`cronward doctor` there.", "kind": "host"})
    elif not stale and v.pop("stale_said", False):
        notices.append({"title": f"cronward on {host} is ticking again", "body": "",
                        "kind": "host"})
    err = st.get("tick_error")
    if err and v.get("error_said") != err:
        notices.append({"title": f"cronward on {host}: a tick failed", "body": clean(err),
                        "kind": "host"})
    v["error_said"] = err

    jobs = {j["name"]: j for j in st["jobs"] if isinstance(j, dict) and "name" in j}
    finished = []
    for name, j in jobs.items():
        final = [r for r in j.get("recent", []) if r.get("kind") != state.PENDING]
        keys = [f"{r['slot']}" for r in final]
        if first:
            finished += final[-1:]
        else:
            old = set(v["seen"].get(name, []))
            finished += [r for r, k in zip(final, keys) if k not in old]
        v["seen"][name] = keys
    notices += changes(v["notified"], finished, jobs)
    state.write_json(_viewer_path(host), v)
    send(notices, cfg)
    return 0
