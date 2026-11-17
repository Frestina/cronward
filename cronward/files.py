"""Reading and writing cronward's own files without being led astray.

A folder is opened once, by walking its path from / one name at a time, and
every file in it is then reached through that open folder (a Folder), never
by its path again. So a folder swapped for a symlink after it was checked
changes nothing.

The walk checks every folder on the way, and every symlink, as ssh's
StrictModes does: each must belong to the user or root, and no one else may
be able to write to it, as whoever can replace a name on the way decides
where the path leads. Two exceptions: a sticky folder (such as /tmp), where
no one can replace what is not theirs, and a folder of the user's that only
its group can write to, when the user is that group's only member (a user
private group). A symlink is followed, and the folders it leads to are
checked the same way.

cronward's own folders (the state, the tokens) must belong to the user, no one
else may write to them, and they may not be symlinks (private_folder).

A file is written to a new temp file with a random name next to it
(O_EXCL, O_NOFOLLOW, 0600), then renamed over the old one, so nothing is
truncated and no symlink is followed. A lock file is opened without
truncating or following a symlink, and a file is read without following one.
"""

import contextlib
import errno
import grp
import os
import pwd
import secrets
import stat
from pathlib import Path
from typing import BinaryIO

DIR = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
LINKS = 40  # as the kernel's limit


class Unsafe(Exception):
    """A folder or file cronward will not use; says why and what to do."""


def private_group(gid: int) -> bool:
    """Whether the user is the group's only member, as in a user private
    group. Group names and members are the system's, so ask the system."""
    uid = os.getuid()
    try:
        group = grp.getgrgid(gid)
        me = pwd.getpwuid(uid).pw_name
    except KeyError:
        return False
    if any(m != me for m in group.gr_mem):
        return False
    found = bool(group.gr_mem)
    for p in pwd.getpwall():
        if p.pw_gid == gid:
            if p.pw_uid != uid:
                return False
            found = True
    return found


def _why(st: os.stat_result, own: bool = False) -> str | None:
    """Why a folder, file or symlink is not safe to go through, or None. own:
    one of cronward's own, which must be the user's, and only the user's to
    write."""
    uid = os.getuid()
    if st.st_uid != uid and (own or st.st_uid != 0):
        return f"belongs to someone else (uid {st.st_uid})"
    if stat.S_ISLNK(st.st_mode):
        return None  # its own mode means nothing
    mode = stat.S_IMODE(st.st_mode)
    if not mode & 0o022:
        return None
    if not own and stat.S_ISDIR(st.st_mode) and mode & stat.S_ISVTX:
        return None
    if not own and not mode & 0o002 and st.st_uid == uid and private_group(st.st_gid):
        return None
    return f"can be written by others (mode {mode:o})"


def _refuse(where: Path, why: str, path: Path) -> Unsafe:
    fix = ("chmod go-w " + str(where) if why.startswith("can be") else
           "keep cronward's files under folders only you or root can change")
    if where == path:
        return Unsafe(f"{where} {why}, so cronward does not use it; fix it with: {fix}")
    return Unsafe(f"{where} {why}, so whoever can change it decides what {path} is, and "
                  f"cronward does not use it; fix it with: {fix}")


def _walk(path: Path, make: bool, about: Path | None = None) -> int:
    """An open descriptor of the folder at path, walked from / with each folder
    and symlink on the way checked. With make, missing folders are made
    (0755, less the umask). about: what the walk is for, for messages."""
    path = Path(os.getcwd(), path)
    about = about or path
    stack = [os.open("/", DIR)]
    real = [Path("/")]  # where each folder on the stack really is
    try:
        if why := _why(os.fstat(stack[0])):
            raise _refuse(Path("/"), why, about)
        todo = list(reversed(path.parts[1:]))
        links = 0
        while todo:
            name = todo.pop()
            if name in ("", "."):
                continue
            if name == "..":
                if len(stack) > 1:
                    os.close(stack.pop())
                    real.pop()
                continue
            here = real[-1] / name
            try:
                st = os.stat(name, dir_fd=stack[-1], follow_symlinks=False)
            except FileNotFoundError:
                if not make:
                    raise FileNotFoundError(errno.ENOENT, "No such file or directory",
                                            str(here)) from None
                with contextlib.suppress(FileExistsError):
                    os.mkdir(name, 0o755, dir_fd=stack[-1])
                todo.append(name)
                continue
            if stat.S_ISLNK(st.st_mode):
                links += 1
                if links > LINKS:
                    raise Unsafe(f"{about} leads through more than {LINKS} symlinks")
                if why := _why(st):
                    raise _refuse(here, f"is a symlink that {why}", about)
                target = Path(os.readlink(name, dir_fd=stack[-1]))
                if target.is_absolute():
                    while len(stack) > 1:
                        os.close(stack.pop())
                        real.pop()
                todo.extend(reversed(target.parts[1:] if target.is_absolute()
                                     else target.parts))
                continue
            if not stat.S_ISDIR(st.st_mode):
                raise Unsafe(f"{here} is not a folder, so {about} cannot be one")
            stack.append(os.open(name, DIR, dir_fd=stack[-1]))
            real.append(here)
            if why := _why(os.fstat(stack[-1])):
                raise _refuse(here, why, about)
        return stack.pop()
    finally:
        for fd in stack:
            os.close(fd)


def _plain(name: str) -> str:
    if not name or "/" in name or name in (".", ".."):
        raise ValueError(f"not a name in a folder: {name!r}")
    return name


class Folder:
    """An open, checked folder. Every name is a file in it, never a path."""

    def __init__(self, path: Path, fd: int):
        self.path = path
        self.fd = fd

    def __enter__(self) -> "Folder":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def close(self) -> None:
        if self.fd >= 0:
            os.close(self.fd)
            self.fd = -1

    def _open(self, name: str, flags: int, mode: int = 0o600) -> int:
        _plain(name)
        try:
            fd = os.open(name, flags | os.O_NOFOLLOW | os.O_CLOEXEC, mode, dir_fd=self.fd)
        except OSError as e:
            if e.errno == errno.ELOOP:
                raise Unsafe(f"{self.path / name} is a symlink, which cronward does not "
                             "follow; move it away") from None
            raise OSError(e.errno, e.strerror, str(self.path / name)) from None
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            os.close(fd)
            raise Unsafe(f"{self.path / name} is not a plain file; move it away")
        return fd

    def open(self, name: str) -> BinaryIO:
        """The file, open to read. FileNotFoundError when it is not there."""
        return os.fdopen(self._open(name, os.O_RDONLY | os.O_NONBLOCK), "rb")

    def read(self, name: str) -> bytes:
        with self.open(name) as f:
            return f.read()

    def read_text(self, name: str) -> str:
        return self.read(name).decode(errors="replace")

    def stat(self, name: str) -> os.stat_result:
        """The name's own stat: a symlink is not followed."""
        return os.stat(_plain(name), dir_fd=self.fd, follow_symlinks=False)

    def exists(self, name: str) -> bool:
        try:
            self.stat(name)
        except FileNotFoundError:
            return False
        return True

    def write(self, name: str, data: str | bytes, mode: int = 0o600) -> None:
        """Replaces name with data, through a new temp file next to it."""
        _plain(name)
        if isinstance(data, str):
            data = data.encode()
        while True:
            tmp = f".{name}.{secrets.token_hex(6)}"
            try:
                fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
                             | os.O_CLOEXEC, 0o600, dir_fd=self.fd)
                break
            except FileExistsError:
                continue
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(data)
                f.flush()
                os.fchmod(f.fileno(), mode)
                os.fsync(f.fileno())
            os.replace(tmp, name, src_dir_fd=self.fd, dst_dir_fd=self.fd)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(tmp, dir_fd=self.fd)
            raise

    def create(self, name: str, data: str, mode: int = 0o600) -> None:
        """Writes a new file. FileExistsError when anything is there, a symlink
        too, even one to a file that is not there."""
        fd = self._open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
        with os.fdopen(fd, "w") as f:
            f.write(data)

    def lock(self, name: str) -> BinaryIO:
        """A lock file to flock: made when it is not there, never truncated, and
        never through a symlink."""
        return os.fdopen(self._open(name, os.O_RDWR | os.O_CREAT), "r+b")

    def unlink(self, name: str) -> bool:
        """Takes name away (a symlink itself, not what it leads to). Whether it
        was there."""
        try:
            os.unlink(_plain(name), dir_fd=self.fd)
        except FileNotFoundError:
            return False
        return True


def open_folder(path: Path, make: bool = False) -> Folder:
    """The folder at path, open, with every folder and symlink on the way
    checked. FileNotFoundError when it is not there, unless make."""
    return Folder(Path(path), _walk(Path(path), make))


def private_folder(path: Path, make: bool = True) -> Folder:
    """One of cronward's own folders, open: made 0700 when it is not there
    (with make), and refused unless it is a folder, not a symlink, of the
    user's that no one else can write to."""
    path = Path(path)
    parent = _walk(path.parent, make, path)
    try:
        try:
            try:
                fd = os.open(path.name, DIR, dir_fd=parent)
            except FileNotFoundError:
                if not make:
                    raise FileNotFoundError(errno.ENOENT, "No such file or directory",
                                            str(path)) from None
                with contextlib.suppress(FileExistsError):
                    os.mkdir(path.name, 0o700, dir_fd=parent)
                fd = os.open(path.name, DIR, dir_fd=parent)
        except NotADirectoryError:  # what O_NOFOLLOW gives for a symlink here
            if stat.S_ISLNK(os.stat(path.name, dir_fd=parent, follow_symlinks=False).st_mode):
                raise Unsafe(f"{path} is a symlink, which cronward does not follow for its "
                             "own folder; move it away, and cronward makes its own") from None
            raise Unsafe(f"{path} is not a folder; move it away") from None
    finally:
        os.close(parent)
    try:
        st = os.fstat(fd)
        if st.st_uid != os.getuid():
            raise Unsafe(f"{path} belongs to someone else (uid {st.st_uid}), so cronward "
                         "does not use it; move it away, and cronward makes its own")
        if st.st_mode & 0o022:
            raise Unsafe(f"{path} can be written by others (mode {stat.S_IMODE(st.st_mode):o}"
                         f"), so cronward does not use it; make it yours alone with: "
                         f"chmod 700 {path}")
    except BaseException:
        os.close(fd)
        raise
    return Folder(path, fd)


def resolve(path: Path, make: bool = False) -> tuple[Folder, str]:
    """For a file that may be a symlink, such as a config kept in a dotfiles
    repo: the folder it really is in, open and checked, and its name there.
    Each symlink on the way is checked, and so is every folder it leads to."""
    path = about = Path(path)
    for _ in range(LINKS):
        folder = Folder(path.parent, _walk(path.parent, make, about))
        try:
            st = folder.stat(path.name)
        except FileNotFoundError:
            return folder, path.name
        if not stat.S_ISLNK(st.st_mode):
            return folder, path.name
        try:
            if why := _why(st):
                raise _refuse(path, f"is a symlink that {why}", about)
            target = os.readlink(path.name, dir_fd=folder.fd)
        finally:
            folder.close()
        path = path.parent / target
    raise Unsafe(f"{path} leads through more than {LINKS} symlinks")


def check_file(folder: Folder, f: BinaryIO, name: str) -> None:
    """Unsafe unless the open file belongs to the user (or root) and no one
    else can write to it: for a file whose text decides what runs, the config."""
    if why := _why(os.fstat(f.fileno())):
        raise _refuse(folder.path / name, why, folder.path / name)
