import io
import json
import os
import time
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

from cronward import __version__, cli, config, state, tokens
from tests.harness import EXPIRES, JOB, REPO, CronwardTest


class CliTest(CronwardTest):
    def run_cli(self, *argv, stdin=""):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch("sys.stdin", io.StringIO(stdin)), redirect_stdout(out), \
                redirect_stderr(err):
            rc = cli.main(list(argv))
        return rc, out.getvalue(), err.getvalue()

    def test_init_then_status(self):
        rc, out, _ = self.run_cli("init")
        self.assertEqual(rc, 0)
        self.assertTrue(config.config_path().exists())
        rc, out, _ = self.run_cli("init")
        self.assertIn("nothing written", out)
        rc, out, _ = self.run_cli("status")
        self.assertEqual(rc, 0)
        self.assertIn("No token for personal", out)

    def test_token_set_checks_with_github_and_never_prints_the_token(self):
        self.config()
        rc, out, err = self.run_cli("token", "set", "personal", stdin="github_pat_secret\n")
        self.assertEqual(rc, 0, err)
        self.assertIn("GitHub user me", out)
        self.assertNotIn("github_pat_secret", out + err)
        self.assertEqual(tokens.load("personal"), "github_pat_secret")
        self.assertEqual(self.gh.calls[0][1], "/user")
        self.assertNotIn("WARNING", out + err)

    def test_token_set_warns_loudly_when_it_cannot_encrypt(self):
        self.config()
        with mock.patch.dict(os.environ, {"CW_SYSTEMD": "255"}):
            rc, out, err = self.run_cli("token", "set", "personal", stdin="github_pat_x\n")
            self.assertEqual(rc, 0, err)
            self.assertIn("not encrypted", out)
            self.assertIn("WARNING: the token for personal is NOT encrypted: this machine has "
                          "systemd 255, and encryption needs 256 or newer.", err)
            rc, out, _ = self.run_cli("doctor")
        self.assertIn("warn  account personal: the token is NOT encrypted (this machine has "
                      "systemd 255", out)

    def test_token_set_replaces_the_old_tokens_expiry(self):
        self.config()
        state.path().parent.mkdir(parents=True, exist_ok=True)
        state.write_json(state.path(), {**state.empty(), "accounts": {
            "personal": {"expires": "2026-11-01 00:00:00 UTC", "warned": "2026-11-01"}}})
        self.run_cli("token", "set", "personal", stdin="github_pat_new\n")
        self.assertEqual(state.load()["accounts"]["personal"], {"expires": EXPIRES})

    def test_an_unexpected_error_exits_2(self):
        self.config()
        with mock.patch("cronward.tick.run", side_effect=KeyError("job")):
            rc, _, err = self.run_cli("tick")
        self.assertEqual(rc, 2)
        self.assertIn("KeyError", err)
        self.assertIn("KeyError", json.loads(state.status_path().read_text())["tick_error"])

    def test_a_refused_token_is_not_stored(self):
        self.gh.token_ok = False
        rc, _, err = self.run_cli("token", "set", "personal", stdin="github_pat_bad\n")
        self.assertEqual(rc, 2)
        self.assertIn("nothing stored", err)
        self.assertIsNone(tokens.kind("personal"))

    def test_a_classic_token_gets_a_note(self):
        _, out, _ = self.run_cli("token", "set", "personal", stdin="ghp_classic\n")
        self.assertIn("not a fine-grained token", out)

    def test_run_tick_status_and_the_notice(self):
        self.config()
        tokens.store("personal", "t0ken")
        self.gh.clock = time.time()
        rc, out, err = self.run_cli("run", "check")
        self.assertEqual(rc, 0, err)
        self.assertIn(f"https://github.com/{REPO}/actions/runs/1001", out)
        rc, out, _ = self.run_cli("status")
        self.assertEqual(rc, 0)
        self.assertIn("running: slot", out)
        self.gh.finish(1001, "failure")
        rc, out, err = self.run_cli("tick")
        self.assertEqual(rc, 0, err)
        self.assertIn("notice: cronward: check failed", out)
        [call] = self.calls("notify-send")
        self.assertIn("--urgency=critical", call)
        rc, out, _ = self.run_cli("status")
        self.assertEqual(rc, 1)
        self.assertIn("failed: started by hand", out)
        rc, out, _ = self.run_cli("status", "--json")
        st = json.loads(out)
        self.assertEqual(st["jobs"][0]["latest"]["kind"], "failed")

    def test_the_notify_command_gets_json(self):
        dump = self.root / "notice.json"
        self.config(JOB + f"\n[notify]\ndesktop = false\ncommand = ['sh', '-c', 'cat > {dump}']\n")
        tokens.store("personal", "t0ken")
        self.gh.no_dispatch.add((REPO, "check.yml"))
        rc, out, _ = self.run_cli("run", "check")
        self.assertEqual(rc, 1)
        self.assertIn("didn't run", out)
        n = json.loads(dump.read_text())
        self.assertEqual(n["kind"], "didnt_run")
        self.assertEqual(self.calls("notify-send"), [])

    def test_a_tick_that_fails_says_so_in_status(self):
        self.config()
        state.path().parent.mkdir(parents=True, exist_ok=True)
        state.path().write_text("{not json")
        rc, _, err = self.run_cli("tick")
        self.assertEqual(rc, 2)
        self.assertIn("does not read", err)
        st = json.loads(state.status_path().read_text())
        self.assertIn("does not read", st["tick_error"])

    def test_a_failing_tick_is_told_once_until_a_tick_works(self):
        self.config()
        state.path().parent.mkdir(parents=True, exist_ok=True)
        titles = []
        for broken in (True, True, False, True):
            if broken:
                state.path().write_text("{not json")
            else:
                state.path().unlink()
            self.run_cli("tick")
            titles.append([c[-2] for c in self.calls("notify-send")])
            for p in (self.root / "calls").glob("notify-send.*"):
                p.unlink()
        self.assertEqual(titles, [["cronward: a tick failed"], [], [],
                                  ["cronward: a tick failed"]])

    def test_version(self):
        out = io.StringIO()
        with redirect_stdout(out), self.assertRaises(SystemExit) as e:
            cli.main(["--version"])
        self.assertEqual(e.exception.code, 0)
        self.assertEqual(out.getvalue(), f"cronward {__version__}\n")

    def test_doctor(self):
        self.config()
        rc, out, _ = self.run_cli("doctor")
        self.assertEqual(rc, 1)
        self.assertIn(f"ok    cronward {__version__}, Python ", out)
        self.assertIn("FAIL  account personal: no token", out)
        tokens.store("personal", "t0ken")
        rc, out, _ = self.run_cli("doctor")
        self.assertEqual(rc, 0, out)
        self.assertIn("ok    account personal: token of GitHub user me", out)
        self.assertIn(f"ok    job check: {REPO} .github/workflows/check.yml on main", out)
        self.assertIn("warn  the timer is not installed", out)
        self.gh.workflows[(REPO, "check.yml")]["state"] = "disabled_manually"
        rc, out, _ = self.run_cli("doctor")
        self.assertEqual(rc, 1)

    def test_workflows(self):
        tokens.store("personal", "t0ken")
        rc, out, _ = self.run_cli("workflows", "personal")
        self.assertEqual(rc, 0)
        self.assertIn(f"{REPO}  check.yml  Check  (active)", out)

    def test_dry_run(self):
        self.config()
        rc, out, _ = self.run_cli("tick", "--dry-run")
        self.assertIn("check: new", out)
        self.assertFalse(state.path().exists())
