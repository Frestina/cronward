import io
import json
import os
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

from cronward import cli, config, files, notify, state, timers, tokens
from cronward.config import ConfigError
from tests.test_watch import ViewerTest, rec

PRECIOUS = '{"precious": true}\n'


class FolderTest(ViewerTest):
    """Another account can change a folder on the way to cronward's: it
    replaces a folder with a symlink, or plants one, and leads cronward to a
    private folder of the user's."""

    def setUp(self):
        super().setUp()
        self.other = self.root / "private"
        self.other.mkdir(mode=0o700)
        self.precious = self.other / notify.copy_name("anvil")
        self.precious.write_text(PRECIOUS)
        self.main_status([rec("2026-11-03 06:00", "passed")])

    def untouched(self):
        self.assertEqual(self.precious.read_text(), PRECIOUS)
        self.assertEqual(sorted(p.name for p in self.other.iterdir()), [self.precious.name])

    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            rc = cli.main(list(argv))
        return rc, out.getvalue(), err.getvalue()

    def shared(self, mode, private_group=False):
        """XDG_STATE_HOME in a folder with mode, which another account could
        write to unless the user is its group's only member."""
        shared = self.root / "shared"
        shared.mkdir()
        os.chmod(shared, mode)
        os.environ["XDG_STATE_HOME"] = str(shared / "state")
        self.addCleanup(os.chmod, shared, 0o700)
        patch = mock.patch.object(files, "private_group", lambda gid: private_group)
        patch.start()
        self.addCleanup(patch.stop)
        return shared


class StateFolderTest(FolderTest):
    def test_a_state_folder_that_is_a_symlink_is_refused(self):
        config.state_dir().parent.mkdir(parents=True)
        config.state_dir().symlink_to(self.other)
        rc, _, err = self.run_cli("watch", "anvil")
        self.assertEqual(rc, 2)
        self.assertIn(f"{config.state_dir()} is a symlink", err)
        self.untouched()
        with self.assertRaisesRegex(files.Unsafe, "is a symlink"):
            notify.copy("anvil")
        self.untouched()

    def test_a_state_folder_swapped_after_it_is_opened_is_not_followed(self):
        folder = config.state_dir()
        with state.locked():
            folder.rename(folder.with_name("moved"))
            folder.symlink_to(self.other)
            state.write_json(notify.copy_name("anvil"), {"fetched": True})
            state.save(state.empty())
        self.untouched()
        moved = folder.with_name("moved")
        self.assertEqual(json.loads((moved / notify.copy_name("anvil")).read_text()),
                         {"fetched": True})
        self.assertTrue((moved / "state.json").exists())

    def test_a_folder_on_the_way_that_others_can_write_to_is_refused(self):
        shared = self.shared(0o770)
        rc, _, err = self.run_cli("watch", "anvil")
        self.assertEqual(rc, 2)
        self.assertIn(f"{shared} can be written by others (mode 770), so whoever can change "
                      f"it decides what {shared / 'state' / 'cronward'} is", err)
        self.assertIn(f"chmod go-w {shared}", err)
        self.assertEqual(list(shared.iterdir()), [])

    def test_a_folder_on_the_way_writable_by_the_users_own_group_is_fine(self):
        self.shared(0o770, private_group=True)
        self.assertEqual(self.watch()[0], 0)
        self.assertTrue(notify.copy_path("anvil").exists())

    def test_a_folder_on_the_way_anyone_can_write_to_is_refused_even_so(self):
        self.shared(0o777, private_group=True)
        with self.assertRaisesRegex(files.Unsafe, r"mode 777"):
            self.watch()

    def test_a_sticky_folder_on_the_way_is_fine(self):
        self.shared(0o1777)
        self.assertEqual(self.watch()[0], 0)
        st = config.state_dir().stat()
        self.assertEqual((st.st_uid, st.st_mode & 0o777), (os.getuid(), 0o700))

    def test_a_symlink_on_the_way_is_followed_and_where_it_leads_is_checked(self):
        safe = self.root / "elsewhere"
        safe.mkdir()
        os.environ["XDG_STATE_HOME"] = str(self.root / "link")
        (self.root / "link").symlink_to(safe)
        self.assertEqual(self.watch()[0], 0)
        self.assertTrue((safe / "cronward" / notify.copy_name("anvil")).exists())

        shared = self.shared(0o770)
        os.environ["XDG_STATE_HOME"] = str(self.root / "link2")
        (self.root / "link2").symlink_to(shared / "state")
        with self.assertRaisesRegex(files.Unsafe, f"^{shared} can be written by others"):
            self.watch()
        self.assertEqual(list(shared.iterdir()), [])

    def test_a_file_in_the_state_folder_that_is_not_plain_is_refused(self):
        with state.locked():
            pass
        os.mkfifo(state.path())
        with self.assertRaisesRegex(files.Unsafe, "is not a plain file"):
            state.load()

    def test_a_name_is_never_a_path(self):
        with self.assertRaises(ValueError):
            state.write_json(str(self.precious), {})
        self.untouched()


class ConfigFolderTest(FolderTest):
    """The config decides what runs, notify.command, so it is read only when no
    one else could have written it."""

    def test_a_config_others_can_write_to_is_not_read(self):
        self.config()
        os.chmod(config.config_path(), 0o666)
        with self.assertRaisesRegex(ConfigError, r"config.toml can be written by others "
                                                 r"\(mode 666\)"):
            config.load()
        rc, _, err = self.run_cli("tick")
        self.assertEqual(rc, 2)
        self.assertIn("cronward does not read the config", err)
        self.assertEqual([c[-2] for c in self.calls("notify-send")],
                         ["cronward: a tick failed"])

    def test_a_config_in_a_folder_others_can_write_to_is_not_read(self):
        self.config()
        dotfiles = self.root / "dotfiles"
        dotfiles.mkdir()
        os.rename(config.config_path(), dotfiles / "cronward.toml")
        config.config_path().symlink_to(dotfiles / "cronward.toml")
        self.assertEqual(config.load().jobs[0].name, "check")
        config.save(config.read_text() + "# kept in the dotfiles\n")
        self.assertTrue(config.config_path().is_symlink())
        self.assertIn("# kept", (dotfiles / "cronward.toml").read_text())

        os.chmod(dotfiles, 0o777)
        self.addCleanup(os.chmod, dotfiles, 0o700)
        with self.assertRaisesRegex(ConfigError, f"{dotfiles} can be written by others"):
            config.load()
        with self.assertRaisesRegex(ConfigError, "cronward does not write the config"):
            config.save("# changed\n")
        self.assertIn("# kept", (dotfiles / "cronward.toml").read_text())


class TokenFolderTest(FolderTest):
    def test_a_token_folder_that_is_a_symlink_is_refused(self):
        self.config()
        tokens.token_dir().parent.mkdir(parents=True)
        tokens.token_dir().symlink_to(self.other)
        with self.assertRaisesRegex(files.Unsafe, "tokens is a symlink"):
            tokens.store("personal", "github_pat_abc")
        with self.assertRaisesRegex(tokens.TokenError, "tokens is a symlink"):
            tokens.load("personal")
        self.untouched()
        with redirect_stdout(io.StringIO()) as out:
            cli.main(["doctor"])
        self.assertIn("tokens is a symlink", out.getvalue())

    def test_systemd_creds_reads_and_writes_no_path(self):
        tokens.store("personal", "github_pat_abc")
        self.assertEqual(tokens.load("personal"), "github_pat_abc")
        for call in self.calls("systemd-creds"):
            if call[1] in ("encrypt", "decrypt"):
                self.assertEqual(call[-2:], ["-", "-"])
        self.assertEqual(sorted(p.name for p in tokens.token_dir().iterdir()),
                         ["personal.cred"])


class UnitFolderTest(FolderTest):
    def test_units_are_not_written_where_others_can_write(self):
        timers.unit_dir().parent.mkdir(parents=True)
        os.chmod(timers.unit_dir().parent, 0o777)
        self.addCleanup(os.chmod, timers.unit_dir().parent, 0o700)
        with self.assertRaisesRegex(timers.TimersError, "can be written by others"):
            timers.install()
        self.assertFalse(timers.unit_dir().exists())
