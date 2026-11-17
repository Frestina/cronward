import io
import json
import os
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

from cronward import cli, config, files, state, timers, tokens
from tests.harness import BIN, CronwardTest


class FilesTest(CronwardTest):
    """Another account that can write to a folder of cronward's plants a
    symlink at a name cronward uses; the file it points at stays as it was."""

    def setUp(self):
        super().setUp()
        self.target = self.root / "precious"
        self.target.write_text("precious\n")

    def plant(self, path):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.symlink_to(self.target)

    def state_folder(self):
        with files.private_folder(config.state_dir()):
            pass
        return config.state_dir()

    def untouched(self):
        self.assertEqual(self.target.read_text(), "precious\n")

    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            rc = cli.main(list(argv))
        return rc, out.getvalue(), err.getvalue()

    def test_state_and_status_are_not_written_through_the_old_temp_names(self):
        folder = self.state_folder()
        for name in (".state.json.tmp", ".status.json.tmp"):
            self.plant(folder / name)
        state.save(state.empty())
        state.write_json(state.STATUS, {"version": 1})
        self.untouched()
        self.assertEqual(json.loads(state.path().read_text())["version"], state.VERSION)
        self.assertTrue((folder / ".state.json.tmp").is_symlink())
        self.assertEqual(sorted(p.name for p in folder.iterdir()),
                         [".state.json.tmp", ".status.json.tmp", "state.json", "status.json"])

    def test_a_symlink_at_a_lock_is_refused_and_not_followed(self):
        folder = self.state_folder()
        self.plant(folder / "lock")
        self.plant(folder / "config-lock")
        with self.assertRaisesRegex(files.Unsafe, "lock is a symlink"):
            with state.locked():
                pass
        with self.assertRaisesRegex(files.Unsafe, "config-lock is a symlink"):
            with config.editing():
                pass
        self.untouched()

    def test_a_lock_that_is_there_is_not_truncated(self):
        lock = self.state_folder() / "lock"
        lock.write_text("left\n")
        with state.locked():
            pass
        self.assertEqual(lock.read_text(), "left\n")

    def test_the_config_is_not_written_through_the_old_temp_name(self):
        self.config()
        self.plant(config.config_path().with_name(".config.toml.tmp"))
        os.chmod(config.config_path(), 0o640)
        config.save("# changed\n")
        self.untouched()
        self.assertEqual(config.config_path().read_text(), "# changed\n")
        self.assertEqual(config.config_path().stat().st_mode & 0o777, 0o640)

    def test_units_are_not_written_through_the_old_temp_names(self):
        for name in timers.units(timers.TICK):
            self.plant(timers.unit_dir() / f".{name}.tmp")
        with redirect_stdout(io.StringIO()):
            self.assertEqual(timers.install(), 0)
        self.untouched()
        service = timers.unit_dir() / "cronward-tick.service"
        self.assertTrue(service.read_text().startswith(timers.MARK))
        self.assertEqual(service.stat().st_mode & 0o777, 0o644)

    def test_a_plain_token_is_not_written_through_the_old_temp_name(self):
        self.plant(tokens.token_dir() / ".personal.token.tmp")
        path = os.pathsep.join(p for p in os.environ["PATH"].split(os.pathsep)
                               if p != str(BIN) and not os.path.exists(f"{p}/systemd-creds"))
        with mock.patch.dict(os.environ, {"PATH": path}):
            tokens.store("personal", "github_pat_abc")
        self.untouched()
        self.assertEqual(tokens.load("personal"), "github_pat_abc")

    def test_an_encrypted_token_is_not_written_through_the_old_temp_name(self):
        self.plant(tokens.token_dir() / ".personal.cred.tmp")
        tokens.store("personal", "github_pat_abc")
        self.untouched()
        self.assertEqual(tokens.load("personal"), "github_pat_abc")
        self.assertEqual(sorted(p.name for p in tokens.token_dir().iterdir()),
                         [".personal.cred.tmp", "personal.cred"])

    def test_the_error_told_marker_is_not_written_through(self):
        self.config()
        folder = self.state_folder()
        state.path().write_text("{not json")
        self.plant(folder / "tick-error-told")
        rc, _, err = self.run_cli("tick")
        self.assertEqual(rc, 2)
        self.untouched()
        self.assertIn("does not read", state.error_told_path().read_text())

    def test_a_state_folder_others_can_write_to_is_refused(self):
        self.config()
        folder = self.state_folder()
        os.chmod(folder, 0o770)
        rc, _, err = self.run_cli("tick")
        self.assertEqual(rc, 2)
        self.assertIn("can be written by others (mode 770)", err)
        self.assertIn(f"chmod 700 {folder}", err)
        self.assertEqual(list(folder.iterdir()), [])
        self.assertEqual([c[-2] for c in self.calls("notify-send")],
                         ["cronward: a tick failed"])
        rc, out, _ = self.run_cli("doctor")
        self.assertEqual(rc, 1)
        self.assertIn("FAIL", out)
        self.assertIn("can be written by others", out)

    def test_a_state_folder_of_someone_else_is_refused(self):
        folder = self.state_folder()
        ino = folder.stat().st_ino
        real = os.fstat

        def fstat(fd):
            st = real(fd)
            if st.st_ino == ino:
                return os.stat_result((st.st_mode, st.st_ino, st.st_dev, st.st_nlink,
                                       os.getuid() + 1, *st[5:10]))
            return st
        with mock.patch("os.fstat", fstat):
            with self.assertRaisesRegex(files.Unsafe, "belongs to someone else"):
                state.save(state.empty())
        self.assertFalse(state.path().exists())

    def test_init_does_not_write_through_a_symlink(self):
        missing = self.root / "dotfiles" / "cronward.toml"
        self.plant(config.config_path())
        config.config_path().unlink()
        config.config_path().symlink_to(missing)
        rc, out, _ = self.run_cli("init")
        self.assertEqual(rc, 2)
        self.assertIn("is a symlink", out)
        self.assertFalse(missing.exists())

        config.config_path().unlink()
        config.config_path().symlink_to(self.target)
        rc, out, _ = self.run_cli("init")
        self.assertEqual(rc, 0)
        self.assertIn("is there already", out)
        self.untouched()
