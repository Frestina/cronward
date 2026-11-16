"""Writing cronward's own files without being led astray by what is there.

Every file is written to a new temp file with a unique name next to it
(O_EXCL, 0600), then renamed over the old one, so nothing is truncated and
no symlink is followed. Lock files are opened without truncating or
following a symlink. The state folder is used only when it belongs to the
user and no one else can write to it.
"""

import contextlib
import errno
import os
import stat
import tempfile
from pathlib import Path
from typing import BinaryIO


class Unsafe(Exception):
    """A folder or file cronward will not use; says why and what to do."""


def check_private(folder: Path) -> None:
    """Unsafe unless folder belongs to the user and only the user can write to it."""
    st = os.stat(folder)
    if not stat.S_ISDIR(st.st_mode):
        raise Unsafe(f"{folder} is not a folder; move it away")
    if st.st_uid != os.getuid():
        raise Unsafe(f"{folder} belongs to someone else (uid {st.st_uid}), so cronward does "
                     "not use it; move it away, and cronward makes its own")
    if st.st_mode & 0o022:
        raise Unsafe(f"{folder} can be written by others (mode {stat.S_IMODE(st.st_mode):o}), "
                     f"so cronward does not use it; make it yours alone with: chmod 700 {folder}")


def private_dir(folder: Path) -> Path:
    """Makes folder (0700) when it is not there, and checks it when it is."""
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    check_private(folder)
    return folder


def write_atomic(path: Path, data: str | bytes, mode: int = 0o600) -> None:
    """Replaces path with data, through a new temp file next to it."""
    if isinstance(data, str):
        data = data.encode()
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        if mode != 0o600:
            os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def open_lock(path: Path) -> BinaryIO:
    """Opens a lock file to flock: made when it is not there, never truncated,
    and never through a symlink."""
    try:
        fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    except OSError as e:
        if e.errno == errno.ELOOP:
            raise Unsafe(f"{path} is a symlink, which cronward does not follow; "
                         "move it away") from None
        raise
    return os.fdopen(fd, "r+b")


def create_new(path: Path, data: str, mode: int = 0o600) -> None:
    """Writes a new file. FileExistsError when anything is there, a symlink
    too, even one to a file that is not there."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                 mode)
    with os.fdopen(fd, "w") as f:
        f.write(data)
