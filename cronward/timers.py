"""The systemd user timers.

On the main machine, the tick: at minute 1 of every 5, and once at start
after a missed time. On a viewer, the watch: hourly at :08, soon enough for
jobs that run daily. The minute keeps clear of the hour and the quarter
hours, where other timers crowd, so they don't ssh into the same machine at
once.

The main machine may run other work, so the services run at low priority
(Nice=19, idle CPU and IO scheduling), with a memory limit and a time limit.
Without lingering, user timers stop when the user logs out; install says so
and never turns it on itself.

Every unit starts with MARK, so uninstall takes away only what install wrote.
"""

import os
import string
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from cronward import config

MARK = "# Written by cronward install; cronward uninstall removes it."
COMMAND = Path(__file__).resolve().parent.parent / "bin" / "cronward"
TIMEOUT = 30
_PLAIN = frozenset(string.ascii_letters + string.digits + "/._-+,:@=~")
QUIET = ("Nice=19", "CPUSchedulingPolicy=idle", "IOSchedulingClass=idle", "MemoryMax=200M")


class TimersError(Exception):
    """The timers could not be installed, removed or read; says why."""


@dataclass(frozen=True)
class Timer:
    name: str
    what: str
    args: tuple[str, ...]
    schedule: tuple[str, ...]
    service: tuple[str, ...]


# A slow GitHub can keep one job's start busy for 4 minutes with its retries, and
# a tick killed halfway leaves the rest of its slots unstarted. A long tick is
# safe: the lock and the timer keep ticks from overlapping.
TICK = Timer("cronward-tick", "start and follow scheduled workflows", ("tick",),
             ("OnCalendar=*:01/5", "Persistent=true", "AccuracySec=10s"),
             (*QUIET, "TimeoutStartSec=20min"))


def watch_timer(host: str) -> Timer:
    return Timer("cronward-watch", f"notify about cronward on {host}", ("watch", host),
                 ("OnCalendar=*:08", "AccuracySec=30s"), ("Nice=10", "TimeoutStartSec=2min"))


NAMES = ("cronward-tick", "cronward-watch")


def unit_dir() -> Path:
    return config.config_home() / "systemd" / "user"


def _word(w: str) -> str:
    odd = sorted(set(w) - _PLAIN)
    if not w or odd or w.startswith("-"):
        raise TimersError(f"cannot write {w!r} into a unit file: systemd would read "
                          f"{''.join(odd) or 'it'} differently")
    return w


def units(t: Timer) -> dict[str, str]:
    cmd = [os.path.abspath(sys.executable), str(COMMAND)]
    return {
        f"{t.name}.service": "\n".join([
            MARK, "[Unit]", f"Description=cronward: {t.what}", "",
            "[Service]", "Type=oneshot",
            "ExecStart=" + " ".join(_word(w) for w in (*cmd, *t.args)),
            *t.service,
            "# 1 means a job's result was not passed, or the main machine was not",
            "# reached: a result, not a failure.",
            "SuccessExitStatus=1", "",
        ]),
        f"{t.name}.timer": "\n".join([
            MARK, "[Unit]", f"Description=cronward: {t.what}", "",
            "[Timer]", *t.schedule, "",
            "[Install]", "WantedBy=timers.target", "",
        ]),
    }


def ours(path: Path) -> bool:
    if path.is_symlink():
        return False
    try:
        with open(path) as f:
            return f.readline().rstrip("\n") == MARK
    except (OSError, UnicodeDecodeError):
        return False


def _run(*argv: str) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(argv, capture_output=True, text=True, stdin=subprocess.DEVNULL,
                              timeout=TIMEOUT)
    except OSError as e:
        raise TimersError(f"cannot run {argv[0]}: {e.strerror}") from None
    except subprocess.TimeoutExpired:
        raise TimersError(f"{' '.join(argv)} gave no answer in {TIMEOUT}s") from None


def _systemctl(*args: str) -> str:
    done = _run("systemctl", "--user", *args)
    if done.returncode != 0:
        lines = (done.stderr or done.stdout).strip().splitlines()
        last = lines[-1] if lines else f"exit {done.returncode}"
        if "Failed to connect to bus" in last:
            # No user manager: not logged in (or only through su), and no lingering.
            raise TimersError(f"cannot reach your systemd user manager ({last}). It runs while "
                              "you are logged in, and always with lingering: log in to this "
                              "machine (ssh or a desktop, not su), or turn lingering on with: "
                              "loginctl enable-linger, then try again")
        raise TimersError(f"systemctl --user {' '.join(args)}: {last}")
    return done.stdout


def linger() -> bool | None:
    try:
        done = _run("loginctl", "show-user", str(os.getuid()), "--property=Linger", "--value")
    except TimersError:
        return None
    answer = done.stdout.strip()
    return {"yes": True, "no": False}.get(answer) if done.returncode == 0 else None


def install(viewer: str | None = None) -> int:
    t = watch_timer(viewer) if viewer else TICK
    texts = units(t)
    where = unit_dir()
    for name in texts:
        path = where / name
        if (path.exists() or path.is_symlink()) and not ours(path):
            raise TimersError(f"{path} is there and cronward did not write it; "
                              "nothing was installed")
    where.mkdir(parents=True, exist_ok=True)
    for name, text in texts.items():
        tmp = where / f".{name}.tmp"
        tmp.write_text(text)
        os.replace(tmp, where / name)
    _systemctl("daemon-reload")
    _systemctl("enable", "--now", f"{t.name}.timer")
    print(f"installed {t.name}.timer: {t.what} ({'; '.join(t.schedule)})")
    if viewer is None:
        on = linger()
        if on is False:
            print("lingering is off, so the timer stops when you log out, unless a session "
                  "stays logged in (autologin). Turn it on with: loginctl enable-linger")
        elif on is None:
            print("cannot tell whether lingering is on; without it the timer stops when you "
                  "log out")
    return 0


def installed() -> list[str]:
    where = unit_dir()
    names = [f"{n}.{ext}" for n in NAMES for ext in ("service", "timer")]
    return [n for n in names if ours(where / n)]


def uninstall() -> int:
    where = unit_dir()
    mine = installed()
    if timers := [n for n in mine if n.endswith(".timer")]:
        _systemctl("disable", "--now", *timers)
    for name in mine:
        (where / name).unlink()
        print(f"removed {where / name}")
    if mine:
        _systemctl("daemon-reload")
        print("config, tokens and state are left; delete them by hand to remove everything")
    else:
        print("no cronward timers installed; nothing removed")
    return 0


def active(name: str) -> str | None:
    """The timer's ActiveState, or None when cronward did not install it. Raises
    TimersError when systemd cannot say."""
    if not ours(unit_dir() / f"{name}.timer"):
        return None
    return _systemctl("show", "--property=ActiveState", "--value", f"{name}.timer").strip()
