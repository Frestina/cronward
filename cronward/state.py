"""The state: ~/.local/state/cronward/state.json.

    {"version": 1,
     "jobs": {"<job>": {"through": <time>, "notified": "<kind>" | null}},
     "accounts": {"<account>": {"expires": "<as GitHub sends it>", "warned": "<date>"}},
     "results": [<result>, ...],
     "last_tick": <time>, "tick_error": "<why>" | null}

A result is one slot of one job:

    {"job", "slot", "kind": "pending" | "passed" | "failed" | "didnt_run",
     "reason", "run_id", "url", "started", "finished",
     "last": <time>, "count": <n>}   # last and count only for missed slots
     "manual": true                  # only for `cronward run`

Times are Unix timestamps. Writes are atomic, and every change happens
under locked(), so a tick and `cronward run` never overlap.
"""

import contextlib
import fcntl
import json
import os
from collections.abc import Iterator
from pathlib import Path

from cronward import config

VERSION = 1
KEEP = 50  # results kept per job
PENDING, PASSED, FAILED, DIDNT_RUN = "pending", "passed", "failed", "didnt_run"
WORDS = {PENDING: "running", PASSED: "passed", FAILED: "failed", DIDNT_RUN: "didn't run"}


class StateError(Exception):
    """The state file does not read; says why."""


class Busy(Exception):
    """Another cronward holds the lock."""


def path() -> Path:
    return config.state_dir() / "state.json"


def status_path() -> Path:
    return config.state_dir() / "status.json"


def error_told_path() -> Path:
    """The last tick error a notice was given for. Kept apart from the state,
    because a state that does not read is one of the errors."""
    return config.state_dir() / "tick-error-told"


def empty() -> dict:
    return {"version": VERSION, "jobs": {}, "accounts": {}, "results": [], "last_tick": None,
            "tick_error": None}


@contextlib.contextmanager
def locked(wait: bool = False) -> Iterator[None]:
    config.state_dir().mkdir(parents=True, exist_ok=True, mode=0o700)
    with open(config.state_dir() / "lock", "w") as f:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | (0 if wait else fcntl.LOCK_NB))
        except BlockingIOError:
            raise Busy("another cronward is running") from None
        yield


def load() -> dict:
    p = path()
    try:
        data = json.loads(p.read_text())
    except FileNotFoundError:
        return empty()
    except (OSError, ValueError) as e:
        raise StateError(f"{p} does not read ({e}); fix it, or move it away to start every "
                         "job afresh") from None
    if not isinstance(data, dict) or data.get("version") != VERSION:
        raise StateError(f"{p} is not a version {VERSION} state file; fix it, or move it away "
                         "to start every job afresh")
    for key, value in empty().items():
        data.setdefault(key, value)
    return data


def write_json(p: Path, data: dict) -> None:
    p.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = p.with_name(f".{p.name}.tmp")
    with open(tmp, "w") as f:
        json.dump(data, f, indent=1, sort_keys=True)
        f.write("\n")
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, p)


def save(data: dict) -> None:
    by_job: dict[str, int] = {}
    kept = []
    for r in reversed(data["results"]):
        n = by_job.get(r["job"], 0)
        if n < KEEP or r["kind"] == PENDING:
            kept.append(r)
        by_job[r["job"]] = n + 1
    data["results"] = kept[::-1]
    write_json(path(), data)


def result(job: str, slot: float, kind: str = PENDING, reason: str = "", **more) -> dict:
    return {"job": job, "slot": slot, "kind": kind, "reason": reason, "run_id": None,
            "url": None, "started": None, "finished": None, **more}


def latest(data: dict, job: str, final: bool = True) -> dict | None:
    """The job's newest result; with final, the newest that is not pending."""
    for r in reversed(data["results"]):
        if r["job"] == job and (not final or r["kind"] != PENDING):
            return r
    return None
