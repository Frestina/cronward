"""`cronward --on HOST COMMAND [ARGS]`: a cronward command on the main machine.

For a viewer, such as a laptop with a bar widget, that changes something on
the main machine: `cronward --on homeserver add ...`. It runs the user's
normal ssh, with their keys and agent, and `ssh -t` when a terminal is
attached, so `token set` can read the token there and `edit` can open an
editor. Without a terminal it runs in batch mode, so it fails rather than
waits for a prompt nobody sees. cronward stays the only thing that does
ssh: a widget calls cronward, never ssh itself.

Only cronward runs on the far side: the command name is checked against
cronward's own commands first, and the arguments are quoted for the far
side's shell. It runs ~/.local/bin/cronward, else cronward on the PATH
there, but only one that gives its version and is FAR_NEEDS or newer, as
an older one may write unsafely; the line saying so is a fixed one that a
widget can match. The exit code is passed through; ssh's own failure (255)
and a refused cronward are 2.

Time limits: ssh gives up on a connection after 10 s, and on one that
stops answering after about 30 s (ServerAlive). Beyond that --on waits for
the far side, as long as it takes. When cronward is stopped (SIGTERM,
SIGHUP, SIGINT), it stops its ssh too, then exits 128 plus the signal.
Stopping the local ssh does not stop the far side's command, so a stopped
add, run or remove may still have finished there.

call() is the one way cronward runs ssh, here and for `watch`: without a
terminal, ssh gets its own process group, so a timeout or a stop reaches
whatever ssh started too, such as a ProxyCommand.
"""

import os
import shlex
import signal
import subprocess
import sys
import time

NOT_FOUND = 127
# The far side's cronward was too old, or gave no version, and the command
# was not run. A code cronward never returns, as NOT_FOUND.
TOO_OLD = 125
# The first cronward whose writes are safe (#52, #53). An older one on the far
# side is not run: a viewer that was updated cannot tell what is there.
FAR_NEEDS = (0, 2, 2)
# A connection that stops answering after login fails in about 30 s, not when
# TCP keepalive gives up two hours later. ssh's own failure, 255.
ALIVE = ("-o", "ServerAliveInterval=10", "-o", "ServerAliveCountMax=3")
SIGNALS = (signal.SIGTERM, signal.SIGHUP, signal.SIGINT)
GRACE = 5  # seconds between TERM and KILL
# The line ssh runs on the far side: find cronward, ask its version, refuse
# one older than FAR_NEEDS or with no version, else run the command; all in
# the one session, as some setups allow only a few connections in a while.
# The version is printed only once it is digits and dots, of at most 6 digits
# each. Splitting with ${r%%.*}, not IFS, so a zsh login shell reads it alike.
FIND = ('c="$HOME/.local/bin/cronward"; [ -x "$c" ] || c="$(command -v cronward)" || '
        'exit {code}; '
        'v="$("$c" --version 2>/dev/null)" || v=; v="${{v#cronward }}"; '
        'case "$v" in ""|.*|*.|*..*|*[!0-9.]*|*[0-9][0-9][0-9][0-9][0-9][0-9][0-9]*) v= ;; esac; '
        'new=; if [ -n "$v" ]; then new=1; r="$v."; for n in {fields}; do '
        'x="${{r%%.*}}"; r="${{r#*.}}"; x="${{x:-0}}"; '
        'if [ "$x" -gt "$n" ]; then break; elif [ "$x" -lt "$n" ]; then new=; break; fi; '
        'done; fi; '
        'if [ -z "$new" ]; then if [ -n "$v" ]; then '
        "printf 'cronward: cronward on %s is %s, older than {needs}: update it there\\n' {host} \"$v\" >&2; "
        "else printf 'cronward: cronward on %s gives no version, so may be older than {needs}: "
        "update it there\\n' {host} >&2; fi; exit {old}; fi; "
        'exec "$c" {args}')


class RemoteError(Exception):
    """--on was given wrongly; says why."""


class Stopped(SystemExit):
    """cronward was told to stop while ssh ran, and ssh has been stopped too.
    Exits 128 plus the signal, as a shell reports it."""

    def __init__(self, signum: int):
        super().__init__(128 + signum)
        self.signum = signum


class _Signal(BaseException):
    pass


def call(argv: list[str], group: bool = True, timeout: float | None = None,
         **popen) -> subprocess.CompletedProcess:
    """Run ssh as subprocess.run would, but on a timeout (TimeoutExpired) or a
    stop (Stopped) stop it first, and with group its whole process group: TERM,
    then KILL after GRACE. What is left in the pipes is not read, so a child
    that holds them open cannot hold cronward. Without group (ssh -t), ssh
    stays in the terminal's group, for its prompts, and only ssh is stopped."""
    box = {"proc": None, "running": False, "caught": []}

    def handler(signum, frame):
        box["caught"].append(signum)
        if box["running"]:
            box["running"] = False
            raise _Signal

    old = {s: signal.signal(s, handler) for s in SIGNALS}
    try:
        proc = box["proc"] = subprocess.Popen(argv, start_new_session=group, **popen)
        try:
            box["running"] = True
            if box["caught"]:
                raise _Signal
            out, err = proc.communicate(timeout=timeout)
            box["running"] = False
        except subprocess.TimeoutExpired:
            box["running"] = False
            _stop(proc, group)
            raise
        except _Signal:
            _stop(proc, group)
            raise Stopped(box["caught"][0]) from None
    finally:
        for s, h in old.items():
            signal.signal(s, h)
    if box["caught"]:
        raise Stopped(box["caught"][0])
    return subprocess.CompletedProcess(argv, proc.returncode, out, err)


def _left(proc: subprocess.Popen, group: bool) -> bool:
    if proc.poll() is None:
        return True
    if not group:
        return False
    try:
        os.killpg(proc.pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        pass
    # A zombie counts for killpg, and one whose reaper never reaps (PID 1 in
    # some containers) would hold the wait for all of GRACE.
    try:
        names = os.listdir("/proc")
    except OSError:
        return True
    for name in names:
        if not name.isdigit():
            continue
        try:
            fields = open(f"/proc/{name}/stat").read().rsplit(")", 1)[1].split()
        except (OSError, IndexError):
            continue
        if len(fields) > 2 and fields[2] == str(proc.pid) and fields[0] != "Z":
            return True
    return False


def _stop(proc: subprocess.Popen, group: bool) -> None:
    end = time.monotonic() + GRACE
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            if group:
                os.killpg(proc.pid, sig)
            else:
                proc.send_signal(sig)
        except (ProcessLookupError, PermissionError):
            pass
        while _left(proc, group) and time.monotonic() < end:
            time.sleep(0.05)
        if not _left(proc, group):
            break
    proc.wait()
    for f in (proc.stdin, proc.stdout, proc.stderr):
        if f:
            f.close()


def split(argv: list[str]) -> tuple[str, list[str]] | None:
    """(host, the command and its arguments) when argv starts with --on."""
    if not argv:
        return None
    first = argv[0]
    if first.startswith("--on="):
        host, rest = first[5:], argv[1:]
    elif first == "--on":
        if len(argv) < 2:
            raise RemoteError("--on needs a host, as you would type it after ssh")
        host, rest = argv[1], argv[2:]
    else:
        return None
    if not host or host.startswith("-"):
        raise RemoteError(f"--on needs a host, not {host!r}")
    return host, rest


def command(host: str, args: list[str], commands: tuple[str, ...], tty: bool) -> list[str]:
    """The ssh command line that runs cronward with args on host."""
    if not args:
        raise RemoteError(f"--on {host} needs a command, such as: cronward --on {host} status")
    if args[0] not in commands:
        raise RemoteError(f"{args[0]!r} is not a cronward command; --on runs only cronward's "
                          f"own: {', '.join(commands)}")
    remote = FIND.format(code=NOT_FOUND, old=TOO_OLD, host=shlex.quote(host),
                         needs=".".join(map(str, FAR_NEEDS)),
                         fields=" ".join(map(str, FAR_NEEDS)), args=shlex.join(args))
    how = ["-t"] if tty else ["-T", "-o", "BatchMode=yes"]
    return ["ssh", *how, "-o", "ConnectTimeout=10", *ALIVE, "--", host, remote]


def run(host: str, args: list[str], commands: tuple[str, ...]) -> int:
    tty = sys.stdin.isatty() and sys.stdout.isatty()
    argv = command(host, args, commands, tty)
    sys.stdout.flush()
    try:
        code = call(argv, group=not tty).returncode
    except OSError as e:
        raise RemoteError(f"cannot run ssh: {e.strerror}") from None
    except Stopped:
        print(f"cronward: stopped, and its ssh to {host} with it; {args[0]} may still have "
              f"finished on {host}", file=sys.stderr)
        raise
    if code == 255:
        print(f"cronward: cannot run cronward on {host}: ssh failed (above)", file=sys.stderr)
        return 2
    if code == NOT_FOUND:
        print(f"cronward: not installed on {host}: no ~/.local/bin/cronward, and no cronward "
              "on the PATH there", file=sys.stderr)
        return 2
    if code == TOO_OLD:  # the far side said which, above
        return 2
    if code < 0:
        return 2
    return code
