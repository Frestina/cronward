"""Where each account's token is kept: ~/.local/share/cronward/tokens/.

Never in the config, which people often keep in their dotfiles. With
systemd-creds (systemd 256 or newer) the token is encrypted with --user, so
the file is only readable by this user on this machine: <account>.cred.
Without it, a plain file only the user can read: <account>.token. That is
the case on Ubuntu 24.04 and Debian 12, so `token set` and `doctor` say it
loudly, with why and what to do.

The folder is one of cronward's own (files.private_folder), and every file
in it is read and written through it: systemd-creds reads and writes the
credential on stdin and stdout, never by a path.
"""

import os
import re
import shutil
import stat
import subprocess
import textwrap
from pathlib import Path

from cronward import config, files

TIMEOUT = 30
NEEDED = 256  # the first systemd with systemd-creds --user


class TokenError(Exception):
    """No usable token; says why and what to do."""


def token_dir() -> Path:
    return config.data_dir() / "tokens"


def _names(account: str) -> tuple[str, str]:
    return f"{account}.cred", f"{account}.token"


def _folder(make: bool = False) -> files.Folder:
    """The token folder, open. FileNotFoundError when it is not there and
    not make; TokenError when it is not safe to use."""
    try:
        return files.private_folder(token_dir(), make=make)
    except files.Unsafe as e:
        raise TokenError(str(e)) from None


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


def can_encrypt() -> bool:
    """Whether this machine has a systemd-creds that can encrypt for a user."""
    version = systemd_version() if shutil.which("systemd-creds") else None
    return version is not None and version >= NEEDED


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
    cred, plain = _names(account)
    with files.private_folder(token_dir()) as d:
        os.fchmod(d.fd, 0o700)  # one that was there may be readable by others
        if shutil.which("systemd-creds"):
            try:
                done = _creds("encrypt", f"--name=cronward-{account}", "-", "-",
                              data=token.encode())
            except (OSError, subprocess.TimeoutExpired) as e:
                done = None
                why = str(e)
            else:
                why = done.stderr.decode(errors="replace").strip()
            if done is not None and done.returncode == 0 and done.stdout:
                d.write(cred, done.stdout)
                d.unlink(plain)
                return f"encrypted with systemd-creds in {token_dir() / cred}", None
            if done is not None and done.returncode == 0:
                why = "it wrote nothing"
            why = why_plain(why.splitlines()[-1] if why else "")
        else:
            why = why_plain()
        d.write(plain, token + "\n")  # 0600 from the start
        d.unlink(cred)
        return f"in {token_dir() / plain}, readable only by you, not encrypted", why


def _read(account: str) -> tuple[bytes | None, bytes | None, int]:
    """The account's credential, or else its plain token with its mode."""
    cred, plain = _names(account)
    try:
        d = _folder()
    except FileNotFoundError:
        return None, None, 0
    try:
        with d:
            try:
                return d.read(cred), None, 0
            except FileNotFoundError:
                pass
            try:
                with d.open(plain) as f:
                    return None, f.read(), os.fstat(f.fileno()).st_mode
            except FileNotFoundError:
                return None, None, 0
    except (files.Unsafe, OSError) as e:
        raise TokenError(f"cannot read the token for {account}: {e}") from None


def load(account: str) -> str:
    sealed, text, mode = _read(account)
    if sealed is not None:
        try:
            done = _creds("decrypt", f"--name=cronward-{account}", "-", "-", data=sealed)
        except (OSError, subprocess.TimeoutExpired) as e:
            raise TokenError(f"cannot decrypt the token for {account}: {e}") from None
        if done.returncode != 0:
            why = done.stderr.decode(errors="replace").strip().splitlines()
            raise TokenError(f"cannot decrypt the token for {account}: "
                             f"{why[-1] if why else 'systemd-creds failed'}; store it again "
                             f"with: cronward token set {account}")
        token = done.stdout.decode(errors="replace").strip()
    elif text is not None:
        if mode & (stat.S_IRWXG | stat.S_IRWXO):
            plain = token_dir() / _names(account)[1]
            raise TokenError(f"{plain} can be read by others; fix with: chmod 600 {plain}")
        token = text.decode(errors="replace").strip()
    else:
        raise TokenError(f"no token for {account}; store one with: cronward token set {account}")
    if not token:
        raise TokenError(f"the token for {account} is empty; store it again with: "
                         f"cronward token set {account}")
    return token


def kind(account: str) -> str | None:
    """"encrypted", "plain" or None, for doctor and status. None too when the
    folder is not safe to use; load and doctor say why."""
    cred, plain = _names(account)
    try:
        with _folder() as d:
            return "encrypted" if d.exists(cred) else "plain" if d.exists(plain) else None
    except (FileNotFoundError, TokenError):
        return None


def check() -> str | None:
    """Why the token folder is not safe to use, or None, for doctor."""
    try:
        _folder().close()
    except FileNotFoundError:
        pass
    except TokenError as e:
        return str(e)
    return None


def remove(account: str) -> bool:
    try:
        d = files.private_folder(token_dir(), make=False)
    except FileNotFoundError:
        return False
    with d:
        found = [d.unlink(name) for name in _names(account)]
    return any(found)
