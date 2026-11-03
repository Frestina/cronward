import json
import time
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from unittest import mock

from cronward import cli, config, notify, state, status
from tests.harness import REPO, CronwardTest, at

NOW = at("2026-11-03 07:08")


def rec(slot, kind, reason=""):
    return {**state.result("check", at(slot), kind, reason), "finished": at(slot) + 300,
            "url": "https://github.com/me/widget/actions/runs/1"}


class ViewerTest(CronwardTest):
    """A main machine anvil, reached through the fake ssh."""

    def main_status(self, recent, last_tick=NOW - 120, error=None):
        path = self.root / "homes" / "anvil" / ".local/state/cronward/status.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({
            "schema": status.SCHEMA, "host": "anvil", "now": NOW, "last_tick": last_tick,
            "tick_error": error, "accounts": [],
            "jobs": [{"name": "check", "account": "personal", "repo": REPO,
                      "workflow": "check.yml", "schedule": "0 6 * * *", "timezone": "UTC",
                      "enabled": True, "next": at("2026-11-04 06:00"), "running": None,
                      "latest": next((r for r in reversed(recent)
                                      if r["kind"] != state.PENDING), None),
                      "recent": recent}]}))

    def watch(self, now=NOW):
        with redirect_stdout(StringIO()), redirect_stderr(StringIO()):
            rc = notify.watch("anvil", config.Config(), now=now)
        titles = [c[-2] for c in self.calls("notify-send")]
        for p in (self.root / "calls").glob("notify-send.*"):
            p.unlink()
        return rc, titles


class WatchTest(ViewerTest):
    def test_first_watch_tells_only_the_latest_result_if_not_passed(self):
        self.main_status([rec("2026-11-01 06:00", "failed"), rec("2026-11-02 06:00", "failed"),
                          rec("2026-11-03 06:00", "didnt_run", "the run ended cancelled")])
        rc, titles = self.watch()
        self.assertEqual(rc, 0)
        self.assertEqual(titles, ["cronward: check didn't run"])
        self.assertEqual(self.watch()[1], [])

    def test_new_results_are_told_on_change(self):
        self.main_status([rec("2026-11-02 06:00", "passed")])
        self.assertEqual(self.watch()[1], [])
        self.main_status([rec("2026-11-02 06:00", "passed"), rec("2026-11-03 06:00", "failed")])
        self.assertEqual(self.watch()[1], ["cronward: check failed"])
        self.main_status([rec("2026-11-02 06:00", "passed"), rec("2026-11-03 06:00", "failed"),
                          rec("2026-11-04 06:00", "passed")])
        self.assertEqual(self.watch()[1], ["cronward: check passes again"])

    def test_the_ssh_call_reads_with_cat_and_no_agent(self):
        self.main_status([])
        self.watch()
        argv = self.calls("ssh")[0]
        self.assertIn("BatchMode=yes", argv)
        self.assertIn("IdentityAgent=none", argv)
        self.assertTrue(argv[-1].startswith("cat -- "))

    def test_a_main_machine_that_stopped_ticking(self):
        self.main_status([], last_tick=NOW - 3600)
        self.assertEqual(self.watch()[1], ["cronward on anvil has stopped ticking"])
        self.assertEqual(self.watch()[1], [])
        self.main_status([], last_tick=NOW - 60)
        self.assertEqual(self.watch()[1], ["cronward on anvil is ticking again"])

    def test_a_failed_tick_is_told_once(self):
        self.main_status([], error="state.json does not read")
        self.assertEqual(self.watch()[1], ["cronward on anvil: a tick failed"])
        self.assertEqual(self.watch()[1], [])

    def test_unreachable_is_told_after_30_minutes_and_when_back(self):
        rc, titles = self.watch(NOW)
        self.assertEqual((rc, titles), (1, []))
        self.assertEqual(self.watch(NOW + 900)[1], [])
        self.assertEqual(self.watch(NOW + 1800)[1], ["cronward: cannot reach anvil"])
        self.assertEqual(self.watch(NOW + 2700)[1], [])
        self.main_status([], last_tick=NOW + 3500)
        self.assertEqual(self.watch(NOW + 3600)[1], ["cronward: anvil is reachable again"])

    def test_offline_says_nothing(self):
        (self.root / "offline").touch()
        for minutes in (0, 60, 120):
            rc, titles = self.watch(NOW + minutes * 60)
            self.assertEqual((rc, titles), (0, []))

    def test_control_characters_from_the_far_side_are_dropped(self):
        self.main_status([rec("2026-11-03 06:00", "failed", "bad\x1b[31m news")])
        with redirect_stdout(StringIO()), redirect_stderr(StringIO()):
            notify.watch("anvil", config.Config(), now=NOW)
        [call] = self.calls("notify-send")
        self.assertIn("bad[31m news", call[-1])
        self.assertNotIn("\x1b", call[-1])

    def test_a_first_watch_that_fails_does_not_replay_the_history(self):
        self.assertEqual(self.watch()[0], 1)
        self.main_status([rec("2026-11-01 06:00", "failed"), rec("2026-11-02 06:00", "passed"),
                          rec("2026-11-03 06:00", "failed")])
        self.assertEqual(self.watch()[1], ["cronward: check failed"])


class StatusHostTest(ViewerTest):
    """`cronward status --host anvil`: the copy watch kept."""

    def status(self, *more, now=NOW + 600):
        out, err = StringIO(), StringIO()
        with mock.patch.object(time, "time", lambda: now), redirect_stdout(out), \
                redirect_stderr(err):
            rc = cli.main(["status", "--host", "anvil", *more])
        return rc, out.getvalue(), err.getvalue()

    def test_no_copy_yet(self):
        rc, out, err = self.status()
        self.assertEqual((rc, out), (2, ""))
        self.assertIn("no status from anvil yet: it is fetched hourly at :08, or now with: "
                      "cronward watch anvil", err)
        self.watch()  # anvil has no home: unreachable
        rc, out, err = self.status()
        self.assertEqual(rc, 2)
        self.assertIn("(last try: cannot reach anvil: ", err)

    def test_the_copy_with_its_viewer_object(self):
        self.main_status([rec("2026-11-03 06:00", "passed")])
        self.watch()
        rc, out, _ = self.status("--json")
        self.assertEqual(rc, 0)
        st = json.loads(out)
        self.assertEqual(st["jobs"][0]["latest"]["kind"], "passed")
        self.assertEqual(st["viewer"], {"host": "anvil", "fetched": NOW, "error": None,
                                        "offline": False, "unreachable_since": None,
                                        "stopped_ticking": False})
        rc, out, _ = self.status()
        self.assertEqual(rc, 0)
        self.assertIn(f"as of {status.when(NOW)} (10 min ago), from anvil\n", out)
        self.assertIn("passed: slot", out)
        self.assertNotIn("cannot reach", out)

    def test_a_failed_job_exits_1(self):
        self.main_status([rec("2026-11-03 06:00", "failed")])
        self.watch()
        self.assertEqual(self.status()[0], 1)

    def test_unreachable_keeps_the_old_copy_and_says_since_when(self):
        self.main_status([rec("2026-11-03 06:00", "passed")])
        self.watch()
        (self.root / "homes" / "anvil" / ".local/state/cronward/status.json").unlink()
        self.watch(NOW + 3600)
        rc, out, _ = self.status("--json", now=NOW + 3700)
        self.assertEqual(rc, 1)
        v = json.loads(out)["viewer"]
        self.assertEqual((v["fetched"], v["unreachable_since"]), (NOW, NOW + 3600))
        self.assertIn("has no cronward status", v["error"])
        rc, out, _ = self.status(now=NOW + 3700)
        self.assertIn(f"cannot reach anvil since {status.when(NOW + 3600)}: anvil has no "
                      "cronward status", out)
        self.main_status([rec("2026-11-03 06:00", "passed")], last_tick=NOW + 7100)
        self.watch(NOW + 7200)
        v = json.loads(self.status("--json", now=NOW + 7300)[1])["viewer"]
        self.assertEqual((v["fetched"], v["error"], v["unreachable_since"]),
                         (NOW + 7200, None, None))

    def test_a_main_machine_that_stopped_ticking_exits_1(self):
        self.main_status([rec("2026-11-03 06:00", "passed")], last_tick=NOW - 3600)
        self.watch()
        rc, out, _ = self.status()
        self.assertEqual(rc, 1)
        self.assertIn("anvil has stopped ticking", out)
        self.assertTrue(json.loads(self.status("--json")[1])["viewer"]["stopped_ticking"])

    def test_offline_keeps_the_copy_and_says_so(self):
        self.main_status([rec("2026-11-03 06:00", "passed")])
        self.watch()
        (self.root / "offline").touch()
        self.watch(NOW + 3600)
        rc, out, _ = self.status(now=NOW + 3700)
        self.assertEqual(rc, 0)
        self.assertIn("this machine is offline", out)
        v = json.loads(self.status("--json")[1])["viewer"]
        self.assertEqual((v["offline"], v["fetched"], v["unreachable_since"]),
                         (True, NOW, None))

    def test_control_characters_in_the_copy_are_dropped(self):
        self.main_status([rec("2026-11-03 06:00", "failed", "bad\x1b[31m news")])
        self.watch()
        out = self.status()[1]
        self.assertIn("bad[31m news", out)
        self.assertNotIn("\x1b", out)
