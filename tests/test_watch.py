import json
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO

from cronward import config, notify, state, status
from tests.harness import REPO, CronwardTest, at

NOW = at("2026-11-03 07:08")


def rec(slot, kind, reason=""):
    return {**state.result("check", at(slot), kind, reason), "finished": at(slot) + 300,
            "url": "https://github.com/me/widget/actions/runs/1"}


class WatchTest(CronwardTest):
    def main_status(self, recent, last_tick=NOW - 120, error=None):
        path = self.root / "homes" / "anvil" / ".local/state/cronward/status.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({
            "schema": status.SCHEMA, "host": "anvil", "now": NOW, "last_tick": last_tick,
            "tick_error": error, "accounts": [],
            "jobs": [{"name": "check", "repo": REPO, "workflow": "check.yml",
                      "recent": recent}]}))

    def watch(self, now=NOW):
        with redirect_stdout(StringIO()), redirect_stderr(StringIO()):
            rc = notify.watch("anvil", config.Config(), now=now)
        titles = [c[-2] for c in self.calls("notify-send")]
        for p in (self.root / "calls").glob("notify-send.*"):
            p.unlink()
        return rc, titles

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
        self.watch()
        body = self.calls("notify-send")
        self.assertEqual(body, [])  # read, then deleted by watch(); check the text instead
        n = notify.changes({}, [rec("2026-11-03 06:00", "failed", "bad\x1b[31m news")],
                           {"check": {"repo": REPO, "workflow": "check.yml"}})
        self.assertNotIn("\x1b", n[0]["body"])
