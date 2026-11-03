"""`cronward --on HOST ...`, through the fake ssh, with the real bin/cronward
on both sides. Only commands that never talk to GitHub run on the far side."""

import io
import os
import subprocess
import sys
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from cronward import cli, remote
from tests.harness import BIN, JOB, CronwardTest

CRONWARD = Path(__file__).resolve().parent.parent / "bin" / "cronward"
COMMANDS = cli.commands()


class RemoteTest(CronwardTest):
    def anvil(self, installed: bool = True, config: str | None = JOB) -> Path:
        home = self.root / "homes" / "anvil"
        if installed:
            (home / ".local/bin").mkdir(parents=True)
            (home / ".local/bin/cronward").symlink_to(CRONWARD)
        if config is not None:
            (home / ".config/cronward").mkdir(parents=True)
            (home / ".config/cronward/config.toml").write_text(config)
        home.mkdir(parents=True, exist_ok=True)
        return home

    def on(self, *argv):
        done = subprocess.run([sys.executable, str(CRONWARD), *argv], capture_output=True,
                              text=True, stdin=subprocess.DEVNULL)
        return done.returncode, done.stdout, done.stderr

    def test_a_command_runs_on_the_far_side(self):
        self.anvil()
        rc, out, err = self.on("--on", "anvil", "status")
        self.assertEqual(rc, 0, err)
        self.assertIn("check: me/widget check.yml", out)
        [argv] = self.calls("ssh")
        self.assertEqual(argv[:4], ["-T", "-o", "BatchMode=yes", "-o"])
        self.assertIn("exec \"$c\" status", argv[-1])

    def test_on_equals_host(self):
        self.anvil()
        self.assertEqual(self.on("--on=anvil", "status")[0], 0)

    def test_the_exit_code_comes_through(self):
        self.anvil()
        rc, _, err = self.on("--on", "anvil", "watch", "nowhere")  # unreachable from anvil
        self.assertEqual(rc, 1, err)
        self.assertIn("cannot reach nowhere", err)
        rc, _, err = self.on("--on", "anvil", "remove", "nosuch")
        self.assertEqual(rc, 2)
        self.assertIn("no job", err)

    def test_arguments_are_quoted_for_the_far_side(self):
        home = self.anvil()
        rc, _, err = self.on("--on", "anvil", "remove", "x; touch pwned", "$(touch pwned2)")
        self.assertEqual(rc, 2)
        self.assertFalse((home / "pwned").exists())
        self.assertFalse((home / "pwned2").exists())

    def test_only_cronward_commands(self):
        self.anvil()
        rc, _, err = self.on("--on", "anvil", "rm", "-rf", "/")
        self.assertEqual(rc, 2)
        self.assertIn("'rm' is not a cronward command", err)
        self.assertEqual(self.calls("ssh"), [])
        for argv in (["--on"], ["--on", "anvil"], ["--on", "-oProxyCommand=x", "status"],
                     ["--on=", "status"]):
            rc, _, err = self.on(*argv)
            self.assertEqual(rc, 2, argv)
        self.assertEqual(self.calls("ssh"), [])

    def test_on_must_come_first(self):
        rc, _, err = self.on("status", "--on", "anvil")
        self.assertEqual(rc, 2)

    def test_not_installed_on_the_far_side(self):
        self.anvil(installed=False)
        with mock.patch.dict(os.environ, {"PATH": f"{BIN}:/usr/bin:/bin"}):
            rc, _, err = self.on("--on", "anvil", "status")
        self.assertEqual(rc, 2)
        self.assertIn("cronward: not installed on anvil: no ~/.local/bin/cronward", err)

    def test_a_host_that_cannot_be_reached(self):
        rc, _, err = self.on("--on", "nowhere", "status")
        self.assertEqual(rc, 2)
        self.assertIn("cannot run cronward on nowhere: ssh failed", err)

    def test_a_terminal_gets_ssh_t_and_no_batch_mode(self):
        argv = remote.command("anvil", ["token", "set", "personal"], COMMANDS, tty=True)
        self.assertEqual(argv[:2], ["ssh", "-t"])
        self.assertNotIn("BatchMode=yes", argv)
        self.assertEqual(argv[-2:-1], ["anvil"])
        self.assertTrue(argv[-1].endswith('exec "$c" token set personal'))

    def test_errors_in_process(self):
        err = io.StringIO()
        with redirect_stdout(io.StringIO()), redirect_stderr(err):
            self.assertEqual(cli.main(["--on", "anvil"]), 2)
        self.assertIn("--on anvil needs a command", err.getvalue())
