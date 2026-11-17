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
failed tick, or not reachable. It keeps what it fetched as
status-<host>.json, which `cronward status --host <host>` shows (copy).
See SPEC.md, "Viewers".
"""

import datetime
import html
import json
import re
import shutil
import subprocess
import sys
import time

from cronward import config, remote, state, status
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
# A connect that times out is tried once more after RETRY seconds: the time
# between Wi-Fi coming back after sleep and a VPN reconnecting. About
# 10 + 30 + 10 s, and at most 30 + 30 + 30 s: within the watch unit's
# TimeoutStartSec=2min.
RETRY = 30
# The kernel's routing tables, IPv4 then IPv6 (all tables), and the
# interfaces that are a VPN's, not a way out of their own.
ROUTES = ("/proc/net/route", "/proc/net/ipv6_route")
VPN = re.compile(r"tailscale|wg|tun")
RTF_UP, RTF_REJECT = 0x0001, 0x0200
# Control characters, and the invisible ones that reorder or hide text
# (bidirectional overrides and isolates, zero-width characters), with which a
# repo's text could make a notice or a line read as something else.
_CONTROL = re.compile("[\x00-\x08\x0b-\x1f\x7f-\x9f\u061c\u200b-\u200f\u202a-\u202e"
                      "\u2060-\u2069\ufeff]")


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
    # A job taken out of the config gets no notices, as on a viewer.
    out = changes(notified, [r for r in finished if r["job"] in jobs], jobs)
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


class _NotReached(Unreachable):
    """ssh did not get through to the host. timed_out: its connect timed out."""

    def __init__(self, text: str, timed_out: bool = False):
        super().__init__(text)
        self.timed_out = timed_out


def has_route() -> bool:
    """Whether this machine has a default route other than through a VPN.
    A VPN such as Tailscale keeps its interface, and the routes to its
    addresses, while Wi-Fi is down, as just after waking from sleep: ssh to
    such an address then times out rather than finds the network
    unreachable. When the routes cannot be read, it says yes."""
    found = False
    for path, ipv6 in ((ROUTES[0], False), (ROUTES[1], True)):
        try:
            with open(path) as f:
                lines = f.read().splitlines()
        except OSError:
            continue
        found = True
        for line in lines if ipv6 else lines[1:]:
            f = line.split()
            try:
                if ipv6:
                    default = len(f) >= 10 and set(f[0]) == {"0"} and f[1] == "00"
                    iface, flags = f[9], int(f[8], 16)
                else:
                    default = len(f) >= 8 and f[1] == f[7] == "00000000"
                    iface, flags = f[0], int(f[3], 16)
            except ValueError:
                continue
            if (default and flags & RTF_UP and not flags & RTF_REJECT and iface != "lo"
                    and not VPN.match(iface)):
                return True
    return not found


def _fetch_once(host: str) -> dict:
    argv = [*SSH, "--", host, REMOTE]
    try:
        done = remote.call(argv, timeout=TIMEOUT, stdin=subprocess.DEVNULL,
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except OSError as e:
        raise Unreachable(f"cannot run ssh: {e.strerror}") from None
    except subprocess.TimeoutExpired:
        raise _NotReached(f"{host} gave no answer in {TIMEOUT}s") from None
    err = done.stderr.decode(errors="replace").strip().splitlines()
    last = clean(err[-1]) if err else f"exit {done.returncode}"
    if done.returncode == 255:
        if any(o in last for o in OFFLINE):
            raise Offline(f"this machine is offline: {last}")
        raise _NotReached(f"cannot reach {host}: {last}", timed_out="timed out" in last)
    if done.returncode != 0:
        if "No such file" in last:
            raise Unreachable(f"{host} has no cronward status: it has never ticked there")
        raise Unreachable(f"cannot read the status on {host}: {last}")
    try:
        data = json.loads(done.stdout)
        status.check(data)
    except ValueError as e:
        raise Unreachable(f"the status on {host} does not read: {clean(e)}") from None
    return data


def fetch(host: str) -> dict:
    """The main machine's status, over one ssh call, or two when the first
    one's connect times out. Not getting through with no default route but a
    VPN's is Offline, not Unreachable."""
    for again in (False, True):
        try:
            return _fetch_once(host)
        except _NotReached as e:
            if not has_route():
                raise Offline(f"this machine is offline: it has no default route but a "
                              f"VPN's ({e})") from None
            if again or not e.timed_out:
                raise
            print(f"{e}; trying again in {RETRY}s", file=sys.stderr)
            time.sleep(RETRY)
    raise AssertionError("unreachable")


def _safe(host: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.@-]", "_", host)


def _viewer_name(host: str) -> str:
    return f"viewer-{_safe(host)}.json"


def copy_name(host: str) -> str:
    """The file in the state folder where a viewer keeps the main machine's
    status it last fetched."""
    return f"status-{_safe(host)}.json"


def copy_path(host: str):
    return config.state_dir() / copy_name(host)


class NoCopy(Exception):
    """No status fetched from the host yet."""


def stopped(st: dict, at: float) -> bool:
    """Whether the main machine had stopped ticking, judged at time at."""
    tick = st.get("last_tick")
    return not isinstance(tick, (int, float)) or at - tick > status.STALE


def copy(host: str) -> dict:
    """The status `watch host` last fetched, plus a viewer object saying how
    fresh it is: {"host", "fetched", "error", "offline", "unreachable_since",
    "stopped_ticking"}. NoCopy when there is none."""
    name = copy_name(host)
    path = copy_path(host)
    v = _load_viewer(host) or {}
    try:
        with state.folder(make=False) as d:
            st = json.loads(d.read(name))
            status.check(st)
            fetched = v.get("fetched") or d.stat(name).st_mtime
    except FileNotFoundError:
        tried = f" (last try: {clean(v['error'])})" if v.get("error") else ""
        raise NoCopy(f"no status from {host} yet{tried}: it is fetched hourly at :08, or now "
                     f"with: cronward watch {host}") from None
    except (OSError, ValueError) as e:
        raise NoCopy(f"{path} does not read ({clean(e)}); fetch it again with: "
                     f"cronward watch {host}") from None
    st["viewer"] = {"host": host, "fetched": fetched,
                    "error": clean(v["error"]) if v.get("error") else None,
                    "offline": bool(v.get("offline")),
                    "unreachable_since": v.get("unreachable_since"),
                    "stopped_ticking": stopped(st, fetched)}
    return st


def _load_viewer(host: str) -> dict | None:
    try:
        return json.loads(state.read_text(_viewer_name(host)))
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
        v.update(offline=True, error=str(e))
        state.write_json(_viewer_name(host), v)
        print(f"{e}; trying again later", file=sys.stderr)
        return 0
    except Unreachable as e:
        v.pop("offline", None)
        v["error"] = str(e)
        since = v.get("unreachable_since") or now
        v["unreachable_since"] = since
        if now - since >= UNREACHABLE_FOR and not v.get("unreachable_said"):
            v["unreachable_said"] = True
            notices.append({"title": f"cronward: cannot reach {host}",
                            "body": f"{e}. Its jobs may not be starting.", "kind": "host"})
        state.write_json(_viewer_name(host), v)
        send(notices, cfg)
        print(e, file=sys.stderr)
        return 1

    state.write_json(copy_name(host), st)
    v["fetched"] = now
    v.pop("offline", None)
    v.pop("error", None)
    if v.pop("unreachable_said", False):
        notices.append({"title": f"cronward: {host} is reachable again", "body": "",
                        "kind": "host"})
    v.pop("unreachable_since", None)
    # Files from before "contacted" have seen jobs; one that only ever
    # failed to reach the host has none.
    first = not v.pop("contacted", bool(v["seen"]))
    v["contacted"] = True

    tick = st.get("last_tick")
    stale = stopped(st, now)
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
    state.write_json(_viewer_name(host), v)
    send(notices, cfg)
    return 0
