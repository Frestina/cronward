"""What `cronward status` shows, and status.json, which a viewer reads.

    {"schema": 1, "host", "now", "last_tick", "tick_error",
     "jobs": [{"name", "account", "repo", "workflow", "schedule", "timezone",
               "enabled", "next", "latest", "running", "recent": [<result>, ...]}],
     "accounts": [{"name", "token", "expires", "expires_soon"}]}

latest is the newest final result, running the newest pending one, and
recent the last RECENT results, oldest first. A viewer's copy, shown by
`status --host`, adds {"viewer": {...}} (notify.copy). A viewer runs check
on what it fetched before it keeps or shows it.
"""

import datetime
import socket
import time

from cronward import cron, state, tokens
from cronward.config import Config
from cronward.state import PASSED, WORDS

SCHEMA = 1
RECENT = 20  # a viewer reads hourly, and a */5 job has 12 results an hour
SOON = 14 * 86400
STALE = 30 * 60  # a main machine whose last tick is older has stopped


def expiry(text: str | None) -> float | None:
    """GitHub's "2026-12-01 00:00:00 UTC" (or with an offset like +0100)."""
    if not text:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S %Z", "%Y-%m-%d %H:%M:%S %z"):
        try:
            t = datetime.datetime.strptime(text, fmt)
        except ValueError:
            continue
        if t.tzinfo is None:
            t = t.replace(tzinfo=datetime.timezone.utc)
        return t.timestamp()
    return None


def build(cfg: Config, data: dict, now: float | None = None) -> dict:
    now = time.time() if now is None else now
    jobs = []
    for job in cfg.jobs:
        mine = [r for r in data["results"] if r["job"] == job.name]
        jobs.append({
            "name": job.name, "account": job.account, "repo": job.repo,
            "workflow": job.workflow, "schedule": job.schedule.text,
            "timezone": job.zone_name, "enabled": job.enabled,
            "next": cron.next_slot(job.schedule, job.zone, now) if job.enabled else None,
            "latest": state.latest(data, job.name),
            "running": next((r for r in reversed(mine) if r["kind"] == state.PENDING), None),
            "recent": mine[-RECENT:],
        })
    accounts = []
    for name in cfg.accounts:
        exp = data["accounts"].get(name, {}).get("expires")
        t = expiry(exp)
        accounts.append({"name": name, "token": tokens.kind(name), "expires": exp,
                         "expires_soon": t is not None and t - now < SOON})
    return {"schema": SCHEMA, "host": socket.gethostname(), "now": now,
            "last_tick": data.get("last_tick"), "tick_error": data.get("tick_error"),
            "jobs": jobs, "accounts": accounts}


# What a status from another machine must hold before a viewer uses it: the
# fields build makes and the viewer reads. "?" allows null.
_TOP = {"host": "str", "now": "time", "last_tick": "time?", "tick_error": "str?"}
_JOB = {"name": "str", "account": "str", "repo": "str", "workflow": "str",
        "schedule": "str", "timezone": "str", "enabled": "bool", "next": "time?"}
_RESULT = {"job": "str", "slot": "time", "kind": "kind", "reason": "str",
           "url": "str??", "finished": "time??", "note": "str??", "manual": "bool??"}
_ACCOUNT = {"name": "str", "token": "str?", "expires": "str?", "expires_soon": "bool"}
_LATEST = 1e11  # past this, a time is no date Python can show


def _ok(value: object, kind: str) -> bool:
    if value is None:
        return kind.endswith("?")
    kind = kind.rstrip("?")
    if kind == "time":
        return (isinstance(value, (int, float)) and not isinstance(value, bool)
                and 0 <= value < _LATEST)  # also refuses NaN
    if kind == "kind":
        return isinstance(value, str) and value in WORDS
    return isinstance(value, {"str": str, "bool": bool}[kind])


def _fields(obj: object, shape: dict[str, str], where: str) -> None:
    if not isinstance(obj, dict):
        raise ValueError(f"{where} is not an object")
    for key, kind in shape.items():
        # "??": may be left out; the viewer reads it with .get.
        if key not in obj and not kind.endswith("??"):
            raise ValueError(f"{where} has no {key}")
        if not _ok(obj.get(key), kind):
            raise ValueError(f"{where}.{key} is {obj[key]!r:.40}")


def check(st: object) -> None:
    """ValueError, saying what is wrong, unless st is a status as build makes
    it. A viewer checks the main machine's before it keeps or shows it."""
    if not isinstance(st, dict):
        raise ValueError("it is not an object")
    if st.get("schema") != SCHEMA:
        raise ValueError(f"schema {st.get('schema')!r:.20}: not a status this version reads")
    _fields(st, _TOP, "the status")
    for key in ("jobs", "accounts"):
        if not isinstance(st.get(key), list):
            raise ValueError(f"the status has no {key} list")
    for i, account in enumerate(st["accounts"]):
        _fields(account, _ACCOUNT, f"accounts[{i}]")
    for i, job in enumerate(st["jobs"]):
        _fields(job, _JOB, f"jobs[{i}]")
        for key in ("latest", "running"):
            if key not in job:
                raise ValueError(f"jobs[{i}] has no {key}")
            if job[key] is not None:
                _fields(job[key], _RESULT, f"jobs[{i}].{key}")
        if not isinstance(job.get("recent"), list):
            raise ValueError(f"jobs[{i}] has no recent list")
        for n, rec in enumerate(job["recent"]):
            _fields(rec, _RESULT, f"jobs[{i}].recent[{n}]")


def write(cfg: Config, data: dict) -> None:
    state.write_json(state.status_path(), build(cfg, data))


def age(seconds: float) -> str:
    seconds = max(0, int(seconds))
    if seconds < 90:
        return f"{seconds} s ago"
    if seconds < 90 * 60:
        return f"{seconds // 60} min ago"
    if seconds < 2 * 86400:
        return f"{seconds // 3600} h ago"
    return f"{seconds // 86400} days ago"


def when(ts: float | None) -> str:
    if ts is None:
        return "never"
    return datetime.datetime.fromtimestamp(ts).strftime("%b %-d %H:%M")


def text(st: dict, now: float | None = None) -> str:
    """st as build makes it, or a viewer's copy with its "viewer" object."""
    now = st["now"] if now is None else now
    tick = st["last_tick"]
    v = st.get("viewer")
    lines = [f"cronward on {st['host']}: last tick "
             + (f"{when(tick)} ({age(now - tick)})" if tick else "never")]
    if v:
        lines.append(f"as of {when(v['fetched'])} ({age(now - v['fetched'])}), "
                     f"from {v['host']}")
        if v["unreachable_since"]:
            lines.append(f"  cannot reach {v['host']} since {when(v['unreachable_since'])}: "
                         f"{v['error']}")
        elif v["offline"]:
            lines.append("  this machine is offline, so this may be out of date")
        if v["stopped_ticking"]:
            lines.append(f"  {v['host']} has stopped ticking: check `cronward doctor` there.")
    elif tick and now - tick > STALE:
        lines.append("  The timer has not ticked for a while: check `cronward doctor`.")
    if st["tick_error"]:
        lines.append(f"  The last tick failed: {st['tick_error']}")
    for a in st["accounts"]:
        if a["token"] is None:
            lines.append(f"  No token for {a['name']}: cronward token set {a['name']}")
        elif a["expires_soon"]:
            lines.append(f"  The token for {a['name']} expires {a['expires']}")
    if not st["jobs"]:
        lines.append("\nNo jobs yet. Add one with `cronward add`; `cronward add --help` says how.")
    for j in st["jobs"]:
        lines.append("")
        head = f"{j['name']}: {j['repo']} {j['workflow']}, {j['schedule']} {j['timezone']}"
        lines.append(head + ("" if j["enabled"] else " (disabled)"))
        r = j["latest"]
        if r is None:
            lines.append("  no result yet")
        else:
            word = WORDS[r["kind"]]
            line = f"  {word}: slot {when(r['slot'])}"
            if r.get("manual"):
                line = f"  {word}: started by hand {when(r['slot'])}"
            if r["reason"]:
                line += f", {r['reason']}"
            lines.append(line)
            if r.get("url"):
                lines.append(f"  {r['url']}")
        if j["running"]:
            lines.append(f"  running: slot {when(j['running']['slot'])}"
                         + (f" {j['running']['url']}" if j["running"].get("url") else ""))
        if j["enabled"]:
            lines.append(f"  next: {when(j['next'])}")
    return "\n".join(lines)


def all_passed(st: dict) -> bool:
    """For the exit code: every enabled job's latest result passed, and, on a
    viewer's copy, the main machine was reached and is ticking."""
    v = st.get("viewer")
    if v and (v["unreachable_since"] or v["stopped_ticking"]):
        return False
    return all(j["latest"] is None or j["latest"]["kind"] == PASSED
               for j in st["jobs"] if j["enabled"])
