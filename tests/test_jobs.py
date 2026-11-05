import io
import threading
import types
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

from cronward import cli, config, state, tokens
from cronward.config import ConfigError
from tests.harness import REPO, CronwardTest

CONFIG = """\
# my settings
timezone = "Europe/Oslo"

[accounts.personal]
[accounts.work]

[jobs.check]
account = "personal"
repo = "me/widget"
workflow = "check.yml"
schedule = "0 6 * * *"

# phone
[notify]
desktop = false
"""
ADD = ("add", "nightly", "--repo", REPO, "--workflow", "check.yml", "--schedule", "30 5 * * *")


class ConfigTextTest(CronwardTest):
    def test_add_then_remove_gives_back_the_same_text(self):
        added = config.with_job(CONFIG, "nightly", {"account": "work", "repo": "me/x",
                                                    "workflow": "a.yml", "schedule": "0 1 * * *"})
        self.assertIn("# my settings", added)
        self.assertEqual(config.parse(added).job("nightly").repo, "me/x")
        self.assertEqual(config.without_job(added, "nightly"), CONFIG)

    def test_remove_keeps_the_comment_above_the_next_table(self):
        new = config.without_job(CONFIG, "check")
        self.assertIn("# phone\n[notify]", new)
        self.assertNotIn("[jobs.check]", new)
        self.assertEqual(config.parse(new).jobs, ())

    def test_values_are_quoted(self):
        new = config.with_job(CONFIG, "odd", {"account": "work", "repo": "me/x",
                                              "workflow": "a.yml", "schedule": "0 1 * * *",
                                              "inputs": {"say \"hi\"": "a\\b\nc", "on": True}})
        self.assertEqual(config.parse(new).job("odd").inputs,
                         {"say \"hi\"": "a\\b\nc", "on": "true"})

    def test_a_job_that_does_not_read_is_not_added(self):
        with self.assertRaises(ConfigError):
            config.with_job(CONFIG, "bad", {"account": "work", "repo": "me/x",
                                            "workflow": "a.yml", "schedule": "* * * * *"})

    def test_a_remove_that_would_change_more_is_refused(self):
        split = CONFIG.replace('schedule = "0 6 * * *"\n',
                               'schedule = "0 6 * * *"\n[jobs.check.inputs]\nlevel = "full"\n')
        self.assertEqual(config.parse(split).job("check").inputs, {"level": "full"})
        with self.assertRaises(ConfigError) as refused:
            config.without_job(split, "check")
        self.assertIn("by hand", str(refused.exception))

    def test_an_account_goes_after_the_last_account(self):
        new = config.with_account(CONFIG, "club")
        self.assertIn("[accounts.work]\n[accounts.club]\n\n[jobs.check]", new)
        self.assertEqual(config.parse(new).accounts, ("personal", "work", "club"))
        self.assertIn("# my settings", new)

    def test_an_account_goes_at_the_end_when_there_is_none(self):
        text = 'timezone = "UTC"\n[notify]\ndesktop = false'
        self.assertEqual(config.with_account(text, "club"),
                         'timezone = "UTC"\n[notify]\ndesktop = false\n\n[accounts.club]\n')
        self.assertEqual(config.with_account("", "club"), "[accounts.club]\n")

    def test_an_account_already_there_changes_nothing(self):
        self.assertEqual(config.with_account(CONFIG, "work"), CONFIG)

    def test_an_account_that_does_not_read_back_is_refused(self):
        inline = 'accounts = { personal = {} }\n'
        with self.assertRaises(ConfigError) as refused:
            config.with_account(inline, "club")
        self.assertIn("by hand", str(refused.exception))

    def test_adding_then_removing_an_account_gives_back_the_same_text(self):
        for text in (CONFIG, config.TEMPLATE, 'timezone = "UTC"\n\n[notify]\ndesktop = false\n',
                     "[accounts.personal]\n", ""):
            with self.subTest(text=text):
                self.assertEqual(config.without_account(config.with_account(text, "club"),
                                                        "club"), text)

    def test_removing_an_account_keeps_tables_apart(self):
        text = 'timezone = "UTC"\n\n[accounts.club]  # the club\n[accounts.work]\n'
        self.assertEqual(config.without_account(text, "club"),
                         'timezone = "UTC"\n\n[accounts.work]\n')
        text = 'timezone = "UTC"\n\n[accounts."club"]\n\n# phone\n[notify]\n'
        self.assertEqual(config.without_account(text, "club"),
                         'timezone = "UTC"\n\n# phone\n[notify]\n')
        new = config.without_account(config.TEMPLATE, "personal")
        self.assertEqual(config.parse(new).accounts, ())
        self.assertIn("with: cronward token set <account>\n\n# One job per workflow", new)

    def test_an_account_a_job_uses_is_not_removed(self):
        with self.assertRaises(ConfigError) as refused:
            config.without_account(CONFIG, "personal")
        self.assertIn("used by check", str(refused.exception))

    def test_an_account_that_cannot_be_found_as_a_table_is_not_removed(self):
        for text in ('accounts = { club = {} }\n', '[accounts]\nclub = {}\n'):
            with self.subTest(text=text), self.assertRaises(ConfigError) as refused:
                config.without_account(text, "club")
            self.assertIn("by hand", str(refused.exception))


class AddRemoveTest(CronwardTest):
    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            rc = cli.main(list(argv))
        return rc, out.getvalue(), err.getvalue()

    def test_add_checks_with_github_and_picks_the_account_that_reaches_the_repo(self):
        self.config(CONFIG)
        tokens.store("work", "t0ken")
        rc, out, err = self.run_cli(*ADD, "--input", "level=full")
        self.assertEqual(rc, 0, err)
        self.assertIn("account work", out)
        self.assertIn("cronward run nightly --wait", out)
        job = config.load().job("nightly")
        self.assertEqual((job.account, job.schedule.text, job.zone_name, job.inputs),
                         ("work", "30 5 * * *", "Europe/Oslo", {"level": "full"}))
        self.assertIn("# phone", config.config_path().read_text())

    def test_a_repo_no_token_reaches_is_not_added_and_says_where_to_fix_it(self):
        self.config(CONFIG)
        tokens.store("personal", "t0ken")
        self.gh.repos.clear()
        before = config.config_path().read_text()
        rc, _, err = self.run_cli(*ADD)
        self.assertEqual(rc, 1)
        self.assertIn("personal: the token cannot see the repo", err)
        self.assertIn("work: no token for work", err)
        self.assertIn(cli.TOKENS_PAGE, err)
        self.assertEqual(config.config_path().read_text(), before)

    def test_two_accounts_that_reach_the_repo_must_be_chosen_between(self):
        self.config(CONFIG)
        tokens.store("personal", "t0ken")
        tokens.store("work", "t0ken")
        rc, _, err = self.run_cli(*ADD)
        self.assertEqual(rc, 2)
        self.assertIn("--account", err)
        self.assertEqual(self.run_cli(*ADD, "--account", "work")[0], 0)

    def test_a_workflow_not_on_the_default_branch_is_not_added(self):
        self.config(CONFIG)
        tokens.store("personal", "t0ken")
        rc, _, err = self.run_cli(*ADD[:4], "--workflow", "new.yml", "--schedule", "0 5 * * *")
        self.assertEqual(rc, 1)
        self.assertIn("must be there", err)
        self.assertIn("There are: check.yml", err)

    def test_wrong_values_are_said_before_github_is_asked(self):
        self.config(CONFIG)
        for argv, want in ((("--schedule", "* * * * *"), "more often"),
                           (("--account", "nobody"), "no account 'nobody'")):
            with self.subTest(want):
                rc, _, err = self.run_cli(*ADD, *argv)
                self.assertEqual(rc, 2)
                self.assertIn(want, err)
        rc, _, err = self.run_cli("add", "check", *ADD[2:])
        self.assertIn("there is a job check already", err)
        self.assertEqual(self.gh.calls, [])

    def test_two_adds_at_once_both_stay(self):
        self.config(CONFIG)
        tokens.store("work", "t0ken")
        self.gh.repos["me/other"] = {"full_name": "me/other", "default_branch": "main"}
        self.gh.workflows[("me/other", "check.yml")] = {"state": "active",
                                                        "path": ".github/workflows/check.yml"}
        # cli.main itself, not run_cli: redirecting stdout in two threads at
        # once would leave it pointing at the wrong place.
        other = threading.Thread(target=cli.main, args=([
            "add", "other", "--repo", "me/other", "--workflow", "check.yml",
            "--schedule", "0 7 * * *"],))
        real = cli._check

        def slow_check(*args):
            # The second add starts while the first asks GitHub.
            if other.ident is None:
                other.start()
                other.join(0.3)
                self.assertTrue(other.is_alive(), "the second add did not wait")
            return real(*args)

        with mock.patch.object(cli, "_check", slow_check), redirect_stdout(io.StringIO()):
            rc, _, err = self.run_cli(*ADD)
            other.join(5)
        self.assertEqual(rc, 0, err)
        self.assertEqual({j.name for j in config.load().jobs}, {"check", "nightly", "other"})

    def test_a_token_without_actions_access_says_so(self):
        self.config(CONFIG)
        tokens.store("work", "t0ken")
        self.gh.script = [("GET", "/actions/workflows/check.yml", 403)]
        rc, _, err = self.run_cli(*ADD)
        self.assertEqual(rc, 1)
        self.assertIn("the token for work cannot read the workflows of me/widget", err)
        self.assertIn("Actions: read and write", err)

    def test_remove(self):
        self.config(CONFIG)
        rc, out, _ = self.run_cli("remove", "check")
        self.assertEqual(rc, 0)
        self.assertEqual(config.load().jobs, ())
        rc, _, err = self.run_cli("remove", "check")
        self.assertEqual(rc, 2)
        self.assertIn("no job 'check'", err)

    def test_a_config_kept_elsewhere_through_a_symlink_stays_a_symlink(self):
        real = self.root / "dotfiles" / "config.toml"
        real.parent.mkdir()
        real.write_text(CONFIG)
        config.config_path().parent.mkdir(parents=True)
        config.config_path().symlink_to(real)
        self.run_cli("remove", "check")
        self.assertTrue(config.config_path().is_symlink())
        self.assertNotIn("[jobs.check]", real.read_text())


class RunWaitTest(CronwardTest):
    def setUp(self):
        super().setUp()
        self.config()
        tokens.store("personal", "t0ken")
        self.now = 1_800_000_000.0
        self.gh.clock = self.now
        # Only cronward's own clock: subprocess sleeps too while it waits for
        # a child, and must not finish the run before it has started.
        clock = types.SimpleNamespace(time=lambda: self.now, sleep=self.sleep)
        for module in ("cronward.cli", "cronward.tick"):
            patch = mock.patch(f"{module}.time", clock)
            patch.start()
            self.addCleanup(patch.stop)
        self.on_sleep = None

    def sleep(self, seconds):
        self.now += seconds
        if self.on_sleep:
            self.on_sleep()

    def run_cli(self, *argv):
        out = io.StringIO()
        with redirect_stdout(out), redirect_stderr(io.StringIO()):
            rc = cli.main(list(argv))
        return rc, out.getvalue()

    def test_wait_follows_the_run_to_its_end(self):
        self.on_sleep = lambda: self.gh.finish(1001, "success")
        rc, out = self.run_cli("run", "check", "--wait")
        self.assertEqual(rc, 0, out)
        self.assertIn("check passed\nhttps://github.com/me/widget/actions/runs/1001", out)
        self.assertEqual(state.latest(state.load(), "check")["kind"], state.PASSED)

    def test_a_failed_run_exits_1_and_gives_the_notice(self):
        self.on_sleep = lambda: self.gh.finish(1001, "failure")
        rc, out = self.run_cli("run", "check", "--wait", "10m")
        self.assertEqual(rc, 1)
        self.assertIn("check failed: the run ended failure", out)
        self.assertEqual([c[-2] for c in self.calls("notify-send")], ["cronward: check failed"])

    def test_run_waits_for_a_tick_to_finish_and_says_so(self):
        done, err = [], io.StringIO()

        def run():
            out = io.StringIO()
            with redirect_stdout(out), redirect_stderr(err):
                done.append((cli.main(["run", "check"]), out.getvalue()))
        with state.locked():
            other = threading.Thread(target=run)
            other.start()
            other.join(0.3)
            self.assertTrue(other.is_alive(), "run did not wait for the lock")
            self.assertIn("waiting for another cronward (a tick) to finish", err.getvalue())
        other.join(5)
        rc, out = done[0]
        self.assertEqual(rc, 0, out)
        self.assertIn("started check", out)

    def test_a_run_still_going_at_the_limit_is_left_to_the_tick(self):
        rc, out = self.run_cli("run", "check", "--wait", "1m")
        self.assertEqual(rc, 1)
        self.assertIn("still running after 1m", out)
        self.assertEqual(state.latest(state.load(), "check", final=False)["kind"],
                         state.PENDING)
        self.assertLessEqual(self.now - 1_800_000_000.0, 75)
