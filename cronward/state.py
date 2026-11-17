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

Times are Unix timestamps. Writes are atomic (files.Folder.write), and
every change happens under locked(), so a tick and `cronward run` never
overlap. While a lock is held, every file in the state folder is reached
through the folder the lock was taken in, opened and checked once.
"""

import contextlib
import fcntl
import json
import sys
import threading
from collections.abc import Iterator
from pathlib import Path

from cronward import config, files

VERSION = 1
KEEP = 50  # results kept per job
PENDING, PASSED, FAILED, DIDNT_RUN = "pending", "passed", "failed", "didnt_run"
WORDS = {PENDING: "running", PASSED: "passed", FAILED: "failed", DIDNT_RUN: "didn't run"}


class StateError(Exception):
    """The state file does not read; says why."""


class Busy(Exception):
    """Another cronward holds the lock."""


STATE = "state.json"
STATUS = "status.json"
# The last tick error a notice was given for. Kept apart from the state,
# because a state that does not read is one of the errors.
TOLD = "tick-error-told"

_held = threading.local()  # .folder: the folder of the lock this thread holds


def path() -> Path:
    return config.state_dir() / STATE


def status_path() -> Path:
    return config.state_dir() / STATUS


def error_told_path() -> Path:
    return config.state_dir() / TOLD


def empty() -> dict:
    return {"version": VERSION, "jobs": {}, "accounts": {}, "results": [], "last_tick": None,
            "tick_error": None}


@contextlib.contextmanager
def folder(make: bool = True) -> Iterator[files.Folder]:
    """The state folder, open and checked: the one the lock held now was taken
    in, or else opened now. Without make, FileNotFoundError when it is not
    there."""
    held = getattr(_held, "folder", None)
    if held is not None:
        yield held
        return
    with files.private_folder(config.state_dir(), make=make) as d:
        yield d


@contextlib.contextmanager
def hold(name: str, wait: bool, waiting: str) -> Iterator[None]:
    """Holds the lock file name in the state folder. Until it is let go, the
    state's files are reached through the folder it was taken in."""
    with folder() as d, d.lock(name) as f:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            if not wait:
                raise Busy("another cronward is running") from None
            print(waiting, file=sys.stderr, flush=True)
            fcntl.flock(f, fcntl.LOCK_EX)
        before = getattr(_held, "folder", None)
        _held.folder = d
        try:
            yield
        finally:
            _held.folder = before


def locked(wait: bool = False) -> contextlib.AbstractContextManager[None]:
    # Seconds as a rule, but a tick may take minutes when GitHub is slow.
    return hold("lock", wait, "waiting for another cronward (a tick) to finish")


def exists(name: str) -> bool:
    try:
        with folder(make=False) as d:
            return d.exists(name)
    except FileNotFoundError:
        return False


def read_text(name: str) -> str:
    """A file in the state folder. FileNotFoundError when it is not there."""
    with folder(make=False) as d:
        return d.read_text(name)


def load() -> dict:
    p = path()
    try:
        data = json.loads(read_text(STATE))
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


def write_json(name: str, data: dict) -> None:
    with folder() as d:
        d.write(name, json.dumps(data, indent=1, sort_keys=True) + "\n")


def save(data: dict) -> None:
    by_job: dict[str, int] = {}
    kept = []
    for r in reversed(data["results"]):
        n = by_job.get(r["job"], 0)
        if n < KEEP or r["kind"] == PENDING:
            kept.append(r)
        by_job[r["job"]] = n + 1
    data["results"] = kept[::-1]
    write_json(STATE, data)


def result(job: str, slot: float, kind: str = PENDING, reason: str = "", **more) -> dict:
    return {"job": job, "slot": slot, "kind": kind, "reason": reason, "run_id": None,
            "url": None, "started": None, "finished": None, **more}


def latest(data: dict, job: str, final: bool = True) -> dict | None:
    """The job's newest result; with final, the newest that is not pending."""
    for r in reversed(data["results"]):
        if r["job"] == job and (not final or r["kind"] != PENDING):
            return r
    return None
