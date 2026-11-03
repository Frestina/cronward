#!/usr/bin/env python3
"""Makes docs/demo/demo.svg, the recording at the top of the README:
`cronward add`, `run --wait` and `status`, with fictional names.

It is real cronward output: the commands run through cli.main against the
tests' FakeGitHub, in a throwaway home, on a clock that starts at a fixed
time and moves only when cronward sleeps. So no network, no token, and the
same file every time; tests/test_demo.py checks that the committed SVG still
matches what cronward prints. Between commands, the installed timer is
played by one quiet `cronward tick`. After a change to what these commands
print, run this again:

    python3 docs/demo/make.py

The SVG is a terminal window whose lines appear by CSS animation, on a loop.
With reduced motion asked for, it shows the last frame and stays.
"""

import contextlib
import html
import io
import os
import shutil
import sys
import tempfile
import time as real_time
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from cronward import cli, github, tokens
from tests.harness import BIN, FakeGitHub, _no_sleep_init, at

SVG = Path(__file__).resolve().parent / "demo.svg"
HOST = "homeserver"
HOME = "/home/me"  # what the throwaway home is shown as
START = at("2026-11-03 14:12")
RUN_TAKES = 95  # seconds the fake run takes on GitHub
CONFIG = """\
timezone = "UTC"

[accounts.personal]
"""
SESSION = [
    ["add", "nightly", "--repo", "me/widget", "--workflow", "check.yml",
     "--schedule", "0 6 * * *"],
    ["run", "nightly", "--wait"],
    ["status"],
]
# Seconds in the recording.
TYPE, ENTER, LINE, AFTER, HOLD = 0.045, 0.4, 0.06, 2.2, 6.0


class Clock:
    """Stands in for the time module in cronward's own modules: time() is the
    fake clock and sleep() moves it, the rest is the real module."""

    def __init__(self, gh: FakeGitHub):
        self.gh = gh
        self.now = START
        self.started: dict[int, float] = {}

    def __getattr__(self, name):
        return getattr(real_time, name)

    def time(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.set(self.now + seconds)

    def set(self, now: float) -> None:
        self.now = self.gh.clock = now
        for run_id, run in self.gh.runs.items():
            began = self.started.setdefault(run_id, now)
            if run["status"] != "completed" and now - began >= RUN_TAKES:
                self.gh.finish(run_id, "success")


class Tape(io.TextIOBase):
    """Collects what cronward prints, with the fake time of each write."""

    def __init__(self, clock: Clock):
        self.clock = clock
        self.writes: list[tuple[float, str]] = []

    def write(self, text: str) -> int:
        if text:
            self.writes.append((self.clock.now, text))
        return len(text)


def session() -> list[tuple[float, str]]:
    """What appears on the terminal, and when: (seconds, text)."""
    with contextlib.ExitStack() as stack:
        tmp = Path(tempfile.mkdtemp(prefix="cronward-demo-"))
        stack.callback(shutil.rmtree, tmp)
        home = tmp / "home"
        home.mkdir()
        (tmp / "calls").mkdir()
        env = {k: v for k, v in os.environ.items()
               if not (k.startswith("XDG_") and k.endswith("_HOME"))}
        env.update(HOME=str(home), CW_FAKE=str(tmp), TZ="UTC",
                   PATH=f"{BIN}{os.pathsep}{os.environ.get('PATH', '')}")
        stack.enter_context(mock.patch.dict(os.environ, env, clear=True))
        real_time.tzset()
        stack.callback(real_time.tzset)

        gh = FakeGitHub()
        gh.next_id = 4170000
        clock = Clock(gh)
        stack.enter_context(mock.patch.object(github.Client, "__init__", _no_sleep_init(gh)))
        for module in ("cli", "tick", "status", "notify", "doctor"):
            stack.enter_context(mock.patch(f"cronward.{module}.time", clock))
        stack.enter_context(mock.patch("socket.gethostname", return_value=HOST))

        path = home / ".config" / "cronward" / "config.toml"
        path.parent.mkdir(parents=True)
        path.write_text(CONFIG)
        tokens.store("personal", "t0ken")

        events: list[tuple[float, str]] = []
        t = 0.5
        for argv in SESSION:
            if argv[0] == "status":
                # The timer's tick at the next :01 or :06, which nobody sees.
                clock.set(clock.now - clock.now % 300 + 360)
                quiet = Tape(clock)
                with contextlib.redirect_stdout(quiet), contextlib.redirect_stderr(quiet):
                    cli.main(["tick"])
                clock.set(clock.now + 20)
            events.append((t, "$ "))
            for ch in "cronward " + " ".join(_shell(a) for a in argv):
                t += TYPE
                events.append((t, ch))
            t += ENTER
            events.append((t, "\n"))
            tape = Tape(clock)
            with contextlib.redirect_stdout(tape), contextlib.redirect_stderr(tape):
                cli.main(argv)
            last = None
            for when, text in tape.writes:
                if last is not None and when > last:
                    t += min(2.5, 0.6 + (when - last) / 60)  # a wait, shortened
                last = when
                for line in text.splitlines(keepends=True):
                    t += LINE
                    events.append((t, line.replace(str(home), HOME)))
            t += AFTER
        events.append((t, "$ "))
        events.append((t + HOLD, ""))
    return events


def _shell(word: str) -> str:
    return f'"{word}"' if " " in word or "*" in word else word


# The window. Sizes in px; a monospace character is about 0.6em wide.
FONT, LINE_H, CHAR_W, PAD, BAR = 14, 20, 8.6, 16, 30
BG, FG, DIM, BAR_BG = "#1e1f29", "#d4d6e4", "#7f849c", "#2a2c39"


def svg(events: list[tuple[float, str]]) -> str:
    """The terminal as an animated SVG. Each state of a row (a prompt, a
    command as it is typed, a line of output) is one <text>, shown from its
    time until the row's next state, or until the loop starts over."""
    total = events[-1][0]
    rows: list[list[tuple[float, str]]] = [[]]
    for t, text in events:
        for piece in text.splitlines(keepends=True):
            body = piece.rstrip("\n")
            if body:
                cur = rows[-1][-1][1] if rows[-1] else ""
                if rows[-1] and rows[-1][-1][0] == t:
                    rows[-1].pop()
                rows[-1].append((t, cur + body))
            if piece.endswith("\n"):
                rows.append([])
    if not rows[-1]:
        rows.pop()
    cols = max((len(s) for r in rows for _, s in r), default=0)
    width = round(2 * PAD + cols * CHAR_W)
    height = BAR + 2 * PAD + len(rows) * LINE_H

    def pct(t: float) -> str:
        return f"{100 * t / total:.3f}".rstrip("0").rstrip(".")

    styles, texts, n = [], [], 0
    for i, states in enumerate(rows):
        y = BAR + PAD + (i + 1) * LINE_H - 5
        for j, (t, text) in enumerate(states):  # an empty row has none: a blank line
            end = states[j + 1][0] if j + 1 < len(states) else None
            frames = f"0%{{opacity:0}}{pct(t)}%{{opacity:1}}"
            if end is not None:
                frames += f"{pct(end)}%{{opacity:0}}"
            styles.append(f"@keyframes k{n}{{{frames}}}.k{n}{{animation-name:k{n}}}")
            last = " z" if end is None else ""
            prompt = ""
            if text.startswith("$ "):
                prompt, text = '<tspan class="p">$ </tspan>', text[2:]
            texts.append(f'<text class="e k{n}{last}" x="{PAD}" y="{y}">'
                         f"{prompt}{html.escape(text, quote=False)}</text>")
            n += 1

    css = (f"text{{font:{FONT}px ui-monospace,SFMono-Regular,Menlo,Consolas,"
           f'"DejaVu Sans Mono",monospace;font-variant-ligatures:none;fill:{FG};'
           f"white-space:pre}}"
           f".p{{fill:{DIM}}}.t{{font-size:12px;fill:{DIM}}}"
           f".e{{opacity:0;animation:{total:.3f}s step-end infinite}}"
           + "".join(styles)
           + "@media (prefers-reduced-motion:reduce){.e{animation:none}.z{opacity:1}}")
    # A plain title bar, as a Linux terminal has, with the user and host.
    title = (f'<text class="t" x="{width / 2:g}" y="{BAR // 2 + 4}" text-anchor="middle">'
             f"me@{HOST}: ~</text>")
    return (f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
            f'viewBox="0 0 {width} {height}" role="img" xml:space="preserve" '
            f'aria-label="cronward add, then cronward run --wait, then cronward status">\n'
            f"<title>cronward add, run --wait and status</title>\n"
            f"<style>{css}</style>\n"
            f'<rect width="{width}" height="{height}" rx="8" fill="{BG}"/>\n'
            f'<path d="M0 8a8 8 0 0 1 8-8h{width - 16}a8 8 0 0 1 8 8v{BAR - 8}H0z" '
            f'fill="{BAR_BG}"/>{title}\n'
            + "\n".join(texts) + "\n</svg>\n")


if __name__ == "__main__":
    SVG.write_text(svg(session()))
    print(f"wrote {SVG}")
