import io
import json
import os
import time
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from cronward import __version__, cli, config, state, status, tokens
from tests.harness import EXPIRES, JOB, REPO, CronwardTest, at


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

    def test_doctor_says_when_a_plain_token_can_now_be_encrypted(self):
        self.config()
        with mock.patch.dict(os.environ, {"CW_SYSTEMD": "255"}):
            self.run_cli("token", "set", "personal", stdin="github_pat_x\n")
        rc, out, _ = self.run_cli("doctor")
        self.assertIn("warn  account personal: the token is NOT encrypted, so anything that "
                      "can read your files can use it. This machine can encrypt it now", out)

    def test_a_token_with_odd_characters_is_refused_and_not_shown(self):
        self.config()
        rc, out, err = self.run_cli("token", "set", "personal",
                                    stdin="\u201cgithub_pat_secret\u201d\n")
        self.assertEqual(rc, 2)
        self.assertIn("not a token", err)
        self.assertNotIn("github_pat_secret", out + err)
        self.assertEqual(self.gh.calls, [])

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

    def test_token_set_adds_a_new_account_to_the_config(self):
        self.config()
        rc, out, err = self.run_cli("token", "set", "work", stdin="github_pat_work\n")
        self.assertEqual(rc, 0, err)
        self.assertIn("note: the config has no [accounts.work] yet", out)
        self.assertIn("added [accounts.work] to", out)
        self.assertEqual(config.load().accounts, ("personal", "work"))
        self.assertEqual(tokens.load("work"), "github_pat_work")
        rc, out, _ = self.run_cli("token", "set", "work", stdin="github_pat_work2\n")
        self.assertEqual(rc, 0)
        self.assertNotIn("[accounts.work]", out)
        self.assertEqual(config.load().accounts, ("personal", "work"))

    def test_a_refused_token_adds_no_account(self):
        self.config()
        before = config.config_path().read_text()
        self.gh.token_ok = False
        rc, _, err = self.run_cli("token", "set", "work", stdin="github_pat_bad\n")
        self.assertEqual(rc, 2)
        self.assertEqual(config.config_path().read_text(), before)
        self.assertIsNone(tokens.kind("work"))

    def test_token_set_without_a_config_writes_none(self):
        rc, out, _ = self.run_cli("token", "set", "work", stdin="github_pat_work\n")
        self.assertEqual(rc, 0)
        self.assertIn("no config at", out)
        self.assertFalse(config.config_path().exists())
        self.assertEqual(tokens.load("work"), "github_pat_work")

    def test_token_set_with_a_config_that_does_not_read_keeps_the_token(self):
        config.config_path().parent.mkdir(parents=True)
        config.config_path().write_text("[accounts.personal\n")
        rc, _, err = self.run_cli("token", "set", "work", stdin="github_pat_work\n")
        self.assertEqual(rc, 2)
        self.assertIn("the token is stored, but [accounts.work] is not in the config", err)
        self.assertEqual(config.config_path().read_text(), "[accounts.personal\n")
        self.assertEqual(tokens.load("work"), "github_pat_work")

    def test_token_remove_undoes_token_set(self):
        self.config()
        state.path().parent.mkdir(parents=True, exist_ok=True)
        state.write_json(state.path(), state.empty())
        before = config.config_path().read_bytes()
        self.run_cli("token", "set", "work", stdin="github_pat_work\n")
        self.assertIn("work", state.load()["accounts"])
        rc, out, err = self.run_cli("token", "remove", "work")
        self.assertEqual(rc, 0, err)
        self.assertIn("removed the token for work", out)
        self.assertIn("removed [accounts.work] from", out)
        self.assertEqual(config.config_path().read_bytes(), before)
        self.assertIsNone(tokens.kind("work"))
        self.assertNotIn("work", state.load()["accounts"])
        rc, out, _ = self.run_cli("token", "remove", "work")
        self.assertEqual(rc, 0)
        self.assertEqual(out, "no token stored for work\n")

    def test_token_remove_keeps_an_account_a_job_uses_and_names_the_job(self):
        self.config()
        tokens.store("personal", "t0ken")
        rc, out, err = self.run_cli("token", "remove", "personal")
        self.assertEqual(rc, 0, err)
        self.assertIn("removed the token for personal", out)
        self.assertIn("kept [accounts.personal]", out)
        self.assertIn("as job check uses it", out)
        self.assertIn("`cronward remove check`", out)
        self.assertEqual(config.load().accounts, ("personal",))
        self.assertIsNone(tokens.kind("personal"))

    def test_token_remove_without_a_config_only_removes_the_token(self):
        tokens.store("work", "t0ken")
        rc, out, _ = self.run_cli("token", "remove", "work")
        self.assertEqual((rc, out), (0, "removed the token for work\n"))
        self.assertFalse(config.config_path().exists())

    def test_token_remove_with_a_config_that_does_not_read_still_removes_the_token(self):
        config.config_path().parent.mkdir(parents=True)
        config.config_path().write_text("[accounts.work\n")
        tokens.store("work", "t0ken")
        rc, _, err = self.run_cli("token", "remove", "work")
        self.assertEqual(rc, 2)
        self.assertIn("the token is removed, but [accounts.work] is not taken out of the config", err)
        self.assertEqual(config.config_path().read_text(), "[accounts.work\n")
        self.assertIsNone(tokens.kind("work"))

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
        status.check(st)

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

    def test_the_plugin_has_the_same_version(self):
        root = Path(__file__).resolve().parent.parent
        plugin = json.loads((root / ".claude-plugin" / "plugin.json").read_text())
        self.assertEqual(plugin["version"], __version__)
        for skill in plugin["skills"]:
            self.assertTrue((root / skill / "SKILL.md").is_file(), skill)

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

    def test_doctor_says_when_notices_reach_no_one(self):
        self.config()
        tokens.store("personal", "t0ken")
        rc, out, _ = self.run_cli("doctor")
        self.assertNotIn("reach no one", out)
        with mock.patch("cronward.doctor.shutil.which", return_value=None):
            rc, out, _ = self.run_cli("doctor")
        self.assertEqual(rc, 0, out)
        self.assertIn("warn  notices from this machine reach no one: notify-send is not "
                      "installed, and notify.command is not set", out)

    def test_no_user_manager_is_said_plainly(self):
        self.config()
        tokens.store("personal", "t0ken")
        self.run_cli("install")
        with mock.patch.dict(os.environ, {"CW_SYSTEMCTL": "nobus"}):
            rc, out, _ = self.run_cli("doctor")
            self.assertEqual(rc, 1)
            self.assertIn("FAIL  cannot reach your systemd user manager (Failed to connect to "
                          "bus: No medium found)", out)
            self.assertIn("loginctl enable-linger", out)
            self.run_cli("uninstall")
            rc, _, err = self.run_cli("install")
        self.assertEqual(rc, 2)
        self.assertIn("cannot reach your systemd user manager", err)

    def test_workflows(self):
        tokens.store("personal", "t0ken")
        rc, out, _ = self.run_cli("workflows", "personal")
        self.assertEqual(rc, 0)
        self.assertIn(f"{REPO}  check.yml  Check  (active)", out)

    def test_workflows_marks_jobs_and_has_json(self):
        self.config(JOB + "\n[accounts.work]\n")
        tokens.store("personal", "t0ken")
        self.gh.workflows[(REPO, "other.yml")] = {
            "id": 8, "name": "Other", "state": "disabled_manually",
            "path": ".github/workflows/other.yml"}
        rc, out, err = self.run_cli("workflows")
        self.assertEqual(rc, 2)  # no token for work
        self.assertIn(f"personal: {REPO}  check.yml  Check  (active, job check)", out)
        self.assertIn(f"personal: {REPO}  other.yml  Other  (disabled_manually)", out)
        self.assertIn("work: ", err)
        rc, out, _ = self.run_cli("workflows", "--json")
        self.assertEqual(rc, 2)
        personal, work = json.loads(out)["accounts"]
        self.assertEqual(personal["error"], None)
        self.assertEqual(personal["repos"], [{"repo": REPO, "error": None, "workflows": [
            {"file": "check.yml", "id": 7, "name": "Check", "state": "active",
             "jobs": ["check"]},
            {"file": "other.yml", "id": 8, "name": "Other", "state": "disabled_manually",
             "jobs": []}]}])
        self.assertEqual((work["account"], work["repos"]), ("work", []))
        self.assertTrue(work["error"])
        rc, out, _ = self.run_cli("workflows", "personal", "--json")
        self.assertEqual(rc, 0)
        self.assertEqual([a["account"] for a in json.loads(out)["accounts"]], ["personal"])

    def test_workflows_with_no_account_needs_a_config(self):
        rc, _, err = self.run_cli("workflows")
        self.assertEqual(rc, 2)
        self.assertIn("no config", err)

    def test_edit_opens_the_editor_then_checks(self):
        self.config()
        with mock.patch.dict(os.environ, {"EDITOR": "sed -i 's/0 6 \\* \\* \\*/0 7 * * */'",
                                          "VISUAL": ""}):
            rc, out, err = self.run_cli("edit")
        self.assertEqual(rc, 0, err)
        self.assertIn("reads: 1 accounts, 1 jobs", out)
        self.assertIn('schedule = "0 7 * * *"', config.config_path().read_text())
        with mock.patch.dict(os.environ, {"VISUAL": "sed -i s/account/acount/"}):
            rc, out, err = self.run_cli("edit")
        self.assertEqual(rc, 2)
        self.assertIn("the config does not read now", err)

    def test_edit_finds_an_editor_without_editor_set(self):
        # Over ssh, $EDITOR from the desktop session is not set, and an
        # Omarchy machine has nvim but no nano or vi.
        self.config()
        bin_dir = self.root / "editors"
        bin_dir.mkdir()
        env = {"EDITOR": "", "VISUAL": "", "PATH": str(bin_dir)}
        with mock.patch.dict(os.environ, env):
            rc, _, err = self.run_cli("edit")
        self.assertEqual(rc, 2)
        self.assertIn("no editor: set $EDITOR (none of nano, nvim, vim, vi", err)
        nvim = bin_dir / "nvim"
        nvim.write_text('#!/bin/sh\necho "# edited in nvim" >> "$1"\n')
        nvim.chmod(0o755)
        with mock.patch.dict(os.environ, env):
            rc, out, err = self.run_cli("edit")
        self.assertEqual(rc, 0, err)
        self.assertIn("# edited in nvim", config.config_path().read_text())

    def test_edit_needs_a_config(self):
        rc, _, err = self.run_cli("edit")
        self.assertEqual(rc, 2)
        self.assertIn("cronward init", err)

    def test_dry_run(self):
        self.config()
        rc, out, _ = self.run_cli("tick", "--dry-run")
        self.assertIn("check: new", out)
        self.assertFalse(state.path().exists())

    def results(self, *recs):
        with state.locked():
            data = state.load()
            data["results"].extend(recs)
            state.save(data)

    def test_history_is_each_result_newest_first(self):
        self.config()
        url = "https://github.com/me/widget/actions/runs/"
        self.results(
            {**state.result("check", at("2026-11-01 06:00"), state.PASSED), "url": url + "1"},
            {**state.result("check", at("2026-11-02 06:00"), state.FAILED,
                            "the run ended failure"), "url": url + "2"},
            state.result("check", at("2026-11-03 06:00"), state.DIDNT_RUN,
                         "missed the slot at Nov 3 06:00"),
            {**state.result("check", at("2026-11-03 09:30"), state.PASSED), "url": url + "4",
             "manual": True},
            {**state.result("check", at("2026-11-04 06:00")), "url": url + "5"},
            state.result("other", at("2026-11-04 07:00"), state.FAILED, "not this job"))
        rc, out, err = self.run_cli("history", "check")
        self.assertEqual(rc, 0, err)
        w = status.when
        self.assertEqual(out.splitlines(), [
            f"{w(at('2026-11-04 06:00')):<12}  running     {url}5",
            f"{w(at('2026-11-03 09:30')):<12}  passed      started by hand  {url}4",
            f"{w(at('2026-11-03 06:00')):<12}  didn't run  missed the slot at Nov 3 06:00",
            f"{w(at('2026-11-02 06:00')):<12}  failed      the run ended failure  {url}2",
            f"{w(at('2026-11-01 06:00')):<12}  passed      {url}1",
        ])
        rc, out, _ = self.run_cli("history", "check", "--json")
        self.assertEqual(rc, 0)
        got = json.loads(out)
        self.assertEqual(got["job"], "check")
        self.assertEqual([r["slot"] for r in got["results"]],
                         [at("2026-11-04 06:00"), at("2026-11-03 09:30"), at("2026-11-03 06:00"),
                          at("2026-11-02 06:00"), at("2026-11-01 06:00")])
        self.assertEqual(got["results"][1]["manual"], True)

    def test_history_keeps_the_last_results_the_state_keeps(self):
        self.config()
        self.results(*(state.result("check", at("2026-11-01 06:00") + i * 86400, state.PASSED)
                       for i in range(state.KEEP + 5)))
        _, out, _ = self.run_cli("history", "check", "--json")
        self.assertEqual(len(json.loads(out)["results"]), state.KEEP)

    def test_history_cleans_text_from_github(self):
        self.config()
        self.results(state.result("check", at("2026-11-02 06:00"), state.FAILED,
                                  "the run ended \x1b]8;;evil\x07failure"))
        _, out, _ = self.run_cli("history", "check")
        self.assertNotIn("\x1b", out)
        self.assertNotIn("\x07", out)
        self.assertIn("failure", out)

    def test_history_of_a_job_with_no_results_and_of_no_job(self):
        self.config()
        rc, out, _ = self.run_cli("history", "check")
        self.assertEqual((rc, out), (0, "no results yet for check\n"))
        rc, _, err = self.run_cli("history", "nope")
        self.assertEqual(rc, 2)
        self.assertIn("no job 'nope'", err)
        self.assertIn("there are: check", err)

    def test_history_of_a_removed_job_says_so(self):
        self.config()
        self.results(state.result("check", at("2026-11-02 06:00"), state.PASSED))
        self.run_cli("remove", "check")
        rc, out, _ = self.run_cli("history", "check")
        self.assertEqual(rc, 0)
        self.assertIn("check is no longer in the config", out)
        self.assertIn("passed", out)

    def test_history_runs_over_on(self):
        self.assertIn("history", cli.commands())
