"""Where each account's token is kept: ~/.local/share/cronward/tokens/.

Never in the config, which people often keep in their dotfiles. With
systemd-creds (systemd 256 or newer) the token is encrypted with --user, so
the file is only readable by this user on this machine: <account>.cred.
Without it, a plain file only the user can read: <account>.token. That is
the case on Ubuntu 24.04 and Debian 12, so `token set` and `doctor` say it
loudly, with why and what to do.
"""

import os
import re
import shutil
import stat
import subprocess
import textwrap
from pathlib import Path

from cronward import config

TIMEOUT = 30
NEEDED = 256  # the first systemd with systemd-creds --user


class TokenError(Exception):
    """No usable token; says why and what to do."""


def token_dir() -> Path:
    return config.data_dir() / "tokens"


def _paths(account: str) -> tuple[Path, Path]:
    return token_dir() / f"{account}.cred", token_dir() / f"{account}.token"


def _creds(*args: str, data: bytes | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(["systemd-creds", "--user", *args], input=data, capture_output=True,
                          timeout=TIMEOUT)


def systemd_version() -> int | None:
    try:
        out = subprocess.run(["systemd-creds", "--version"], capture_output=True, text=True,
                             timeout=TIMEOUT).stdout
    except (OSError, subprocess.TimeoutExpired):
        return None
    m = re.match(r"systemd (\d+)", out)
    return int(m[1]) if m else None


def why_plain(error: str = "") -> str:
    """Why a token is not encrypted on this machine, as far as can be told."""
    if not shutil.which("systemd-creds"):
        return "systemd-creds is not installed"
    version = systemd_version()
    if version is not None and version < NEEDED:
        return f"this machine has systemd {version}, and encryption needs {NEEDED} or newer"
    return f"systemd-creds could not encrypt it: {error or 'no reason given'}"


def warning(account: str, why: str) -> str:
    """What `token set` says when the token is stored as a plain file."""
    body = (f"It is a file only you can read, but anything that can read your files, such "
            "as a program running as you or a backup of your home, can use it to start and "
            "read workflows, and turn them on and off, in every repo the token reaches. Keep "
            "the token to the repos of your jobs with Actions: read and write only, and give "
            f"it an expiry date. To encrypt it: move to systemd {NEEDED} or newer (Debian 13, "
            "Ubuntu 24.10, Fedora 41, Arch, or later), then store it again with: "
            f"cronward token set {account}")
    return "\n".join([f"WARNING: the token for {account} is NOT encrypted: {why}.",
                      *textwrap.wrap(body, 76, initial_indent="  ", subsequent_indent="  ")])


def store(account: str, token: str) -> tuple[str, str | None]:
    """Stores the token. Says where, and why it is not encrypted (None if it is)."""
    cred, plain = _paths(account)
    token_dir().mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(token_dir(), 0o700)  # mkdir leaves a folder that was there as it was
    old = os.umask(0o077)
    try:
        if shutil.which("systemd-creds"):
            tmp = cred.with_name(f".{cred.name}.tmp")
            try:
                done = _creds("encrypt", f"--name=cronward-{account}", "-", str(tmp),
                              data=token.encode())
            except (OSError, subprocess.TimeoutExpired) as e:
                done = None
                why = str(e)
            else:
                why = done.stderr.decode(errors="replace").strip()
            if done is not None and done.returncode == 0:
                os.replace(tmp, cred)
                plain.unlink(missing_ok=True)
                return f"encrypted with systemd-creds in {cred}", None
            tmp.unlink(missing_ok=True)
            why = why_plain(why.splitlines()[-1] if why else "")
        else:
            why = why_plain()
        tmp = plain.with_name(f".{plain.name}.tmp")
        tmp.write_text(token + "\n")  # 0600 from the start, by the umask
        os.chmod(tmp, 0o600)
        os.replace(tmp, plain)
        cred.unlink(missing_ok=True)
        return f"in {plain}, readable only by you, not encrypted", why
    finally:
        os.umask(old)


def load(account: str) -> str:
    cred, plain = _paths(account)
    if cred.exists():
        try:
            done = _creds("decrypt", f"--name=cronward-{account}", str(cred), "-")
        except (OSError, subprocess.TimeoutExpired) as e:
            raise TokenError(f"cannot decrypt the token for {account}: {e}") from None
        if done.returncode != 0:
            why = done.stderr.decode(errors="replace").strip().splitlines()
            raise TokenError(f"cannot decrypt the token for {account}: "
                             f"{why[-1] if why else 'systemd-creds failed'}; store it again "
                             f"with: cronward token set {account}")
        token = done.stdout.decode().strip()
    elif plain.exists():
        mode = plain.stat().st_mode
        if mode & (stat.S_IRWXG | stat.S_IRWXO):
            raise TokenError(f"{plain} can be read by others; fix with: chmod 600 {plain}")
        token = plain.read_text().strip()
    else:
        raise TokenError(f"no token for {account}; store one with: cronward token set {account}")
    if not token:
        raise TokenError(f"the token for {account} is empty; store it again with: "
                         f"cronward token set {account}")
    return token


def kind(account: str) -> str | None:
    """"encrypted", "plain" or None, for doctor."""
    cred, plain = _paths(account)
    return "encrypted" if cred.exists() else "plain" if plain.exists() else None


def remove(account: str) -> bool:
    found = False
    for p in _paths(account):
        if p.exists():
            p.unlink()
            found = True
    return found
