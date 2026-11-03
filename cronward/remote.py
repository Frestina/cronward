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
there. The exit code is passed through; ssh's own failure (255) is 2.
"""

import shlex
import subprocess
import sys

NOT_FOUND = 127
FIND = ('c="$HOME/.local/bin/cronward"; [ -x "$c" ] || c="$(command -v cronward)" || '
        'exit {code}; exec "$c" {args}')


class RemoteError(Exception):
    """--on was given wrongly; says why."""


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
    remote = FIND.format(code=NOT_FOUND, args=shlex.join(args))
    how = ["-t"] if tty else ["-T", "-o", "BatchMode=yes"]
    return ["ssh", *how, "-o", "ConnectTimeout=10", "--", host, remote]


def run(host: str, args: list[str], commands: tuple[str, ...]) -> int:
    argv = command(host, args, commands, sys.stdin.isatty() and sys.stdout.isatty())
    sys.stdout.flush()
    try:
        code = subprocess.run(argv).returncode
    except OSError as e:
        raise RemoteError(f"cannot run ssh: {e.strerror}") from None
    if code == 255:
        print(f"cronward: cannot run cronward on {host}: ssh failed (above)", file=sys.stderr)
        return 2
    if code == NOT_FOUND:
        print(f"cronward: not installed on {host}: no ~/.local/bin/cronward, and no cronward "
              "on the PATH there", file=sys.stderr)
        return 2
    if code < 0:
        return 2
    return code
