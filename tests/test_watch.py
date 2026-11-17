import json
import os
import subprocess
import time
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from unittest import mock

from cronward import cli, config, notify, remote, state, status
from tests.harness import REPO, CronwardTest, HangTest, at

NOW = at("2026-11-03 07:08")


def rec(slot, kind, reason=""):
    return {**state.result("check", at(slot), kind, reason), "finished": at(slot) + 300,
            "url": "https://github.com/me/widget/actions/runs/1"}


class ViewerTest(HangTest):
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

    def test_a_host_that_stops_answering_is_unreachable_and_nothing_is_left(self):
        self.hang("anvil")
        start = time.monotonic()
        with mock.patch.object(notify, "TIMEOUT", 1), self.assertRaises(notify.Unreachable) as e:
            notify.fetch("anvil")
        self.assertLess(time.monotonic() - start, 5)
        self.assertEqual(str(e.exception), "anvil gave no answer in 1s")
        self.assertEqual(self.left(), [])

    def test_offline_says_nothing(self):
        (self.root / "offline").touch()
        for minutes in (0, 60, 120):
            rc, titles = self.watch(NOW + minutes * 60)
            self.assertEqual((rc, titles), (0, []))

    def time_out(self, times):
        (self.root / "timeouts").mkdir(exist_ok=True)
        (self.root / "timeouts" / "anvil").write_text(f"{times}\n")

    def viewer(self):
        return json.loads(state.read_text("viewer-anvil.json"))

    def test_a_timeout_with_no_route_but_a_vpn_is_offline(self):
        self.main_status([rec("2026-11-03 06:00", "passed")])
        for ifaces in (("tailscale0",), ("wg0", "tun1"), ()):
            with self.subTest(ifaces):
                self.routes(*ifaces)
                self.time_out(1)
                self.assertEqual(self.watch(), (0, []))
                v = self.viewer()
                self.assertTrue(v["offline"])
                self.assertNotIn("unreachable_since", v)
                self.assertIn("no default route but a VPN's", v["error"])
                self.assertIn("Connection timed out", v["error"])
        self.assertEqual(len(self.calls("ssh")), 3)  # no second try

    def test_a_timeout_then_an_answer_is_a_normal_fetch(self):
        self.main_status([rec("2026-11-03 06:00", "failed")])
        self.time_out(1)
        err = StringIO()
        with redirect_stderr(err):
            rc = notify.watch("anvil", config.Config(), now=NOW)
        self.assertEqual(rc, 0)
        self.assertIn("Connection timed out; trying again in 0s", err.getvalue())
        self.assertEqual(len(self.calls("ssh")), 2)
        self.assertEqual([c[-2] for c in self.calls("notify-send")], ["cronward: check failed"])
        v = self.viewer()
        self.assertEqual(v["fetched"], NOW)
        self.assertFalse({"error", "offline", "unreachable_since"} & set(v))

    def test_two_timeouts_are_unreachable(self):
        self.main_status([])
        self.time_out(2)
        self.assertEqual(self.watch(), (1, []))
        self.assertEqual(len(self.calls("ssh")), 2)
        v = self.viewer()
        self.assertEqual(v["unreachable_since"], NOW)
        self.assertNotIn("offline", v)
        self.assertEqual(v["error"], "cannot reach anvil: ssh: connect to host anvil port 22: "
                                     "Connection timed out")

    def test_the_second_try_waits(self):
        self.main_status([])
        self.time_out(1)
        with mock.patch.object(notify, "RETRY", 0.5):
            start = time.monotonic()
            self.assertEqual(self.watch()[0], 0)
        self.assertGreaterEqual(time.monotonic() - start, 0.5)

    def test_other_failures_are_not_tried_again(self):
        self.assertEqual(self.watch()[0], 1)  # anvil has no home: no route to host
        self.assertEqual(len(self.calls("ssh")), 1)

    def test_no_route_to_the_host_with_no_route_out_is_offline(self):
        self.routes("tailscale0")
        self.assertEqual(self.watch(), (0, []))
        self.assertTrue(self.viewer()["offline"])

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


class RouteTest(CronwardTest):
    """notify.has_route, from the kernel's tables."""

    V6 = ("00000000000000000000000000000000 00 00000000000000000000000000000000 00 "
          "fe800000000000000000000000000001 00000400 00000001 00000000 {flags} {iface}\n")

    def v6(self, *routes):
        (self.root / "ipv6_route").write_text(
            "".join(self.V6.format(flags=f, iface=i) for f, i in routes))

    def test_ipv4(self):
        self.assertTrue(notify.has_route())
        self.routes("tailscale0", "wg0", "tun0")
        self.assertFalse(notify.has_route())
        self.routes("tailscale0", "enp3s0")
        self.assertTrue(notify.has_route())

    def test_ipv6(self):
        self.routes()
        self.v6(("00200200", "lo"), ("00000003", "tailscale0"), ("00000002", "wlan0"))
        self.assertFalse(notify.has_route())  # a reject, a VPN, and one not up
        self.v6(("00200200", "lo"), ("00000003", "wlan0"))
        self.assertTrue(notify.has_route())

    def test_a_route_to_somewhere_is_not_a_default(self):
        (self.root / "route").write_text(
            "Iface\tDestination\tGateway\tFlags\tRefCnt\tUse\tMetric\tMask\n"
            "wlan0\t0001A8C0\t00000000\t0001\t0\t0\t600\t00FFFFFF\n")
        self.assertFalse(notify.has_route())

    def test_tables_that_do_not_read_say_yes(self):
        (self.root / "route").unlink()
        (self.root / "ipv6_route").unlink()
        self.assertTrue(notify.has_route())


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


class ReadOnlyKeyTest(CronwardTest):
    """The README's authorized_keys line for a viewer key that only reads the status."""

    def forced(self, original):
        readme = (Path(__file__).parents[1] / "README.md").read_text()
        line = next(l for l in readme.splitlines() if l.startswith('restrict,command="'))
        command = line[len('restrict,command="'):line.rindex('" ssh-ed25519 ')]
        path = self.home / ".local/state/cronward/status.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('{"host": "anvil"}')
        # As sshd runs it: in the home folder, through the login shell.
        return subprocess.run(["sh", "-c", command], cwd=self.home, capture_output=True,
                              text=True, env={"PATH": os.environ["PATH"],
                                              "SSH_ORIGINAL_COMMAND": original})

    def test_the_viewer_reads_the_status(self):
        r = self.forced(notify.REMOTE)
        self.assertEqual((r.returncode, r.stdout, r.stderr), (0, '{"host": "anvil"}', ""))

    def test_anything_else_is_refused(self):
        on = remote.command("anvil", ["run", "check"], cli.commands(), tty=False)[-1]
        for original in (on, "sh", ""):
            r = self.forced(original)
            self.assertEqual(r.returncode, 1, original)
            self.assertEqual(r.stdout, "")
            self.assertIn("this key only reads the status", r.stderr)


class BadStatusTest(ViewerTest):
    """A status from the main machine that has the schema but not the fields
    (#37): told as not reading, never kept, never a traceback."""

    BAD = {
        "no host": lambda st: st.pop("host"),
        "last_tick a string": lambda st: st.update(last_tick="2026-11-03T07:00:00+01:00"),
        "now not a number": lambda st: st.update(now=float("nan")),
        "no accounts": lambda st: st.pop("accounts"),
        "a job not an object": lambda st: st["jobs"].append("check"),
        "latest a string": lambda st: st["jobs"][0].update(latest="passed"),
        "a result of no kind": lambda st: st["jobs"][0]["recent"][0].update(kind="fine"),
        "a kind that is a list": lambda st: st["jobs"][0]["recent"][0].update(kind=["passed"]),
        "a slot past any date": lambda st: st["jobs"][0]["recent"][0].update(slot=1e300),
        "enabled a string": lambda st: st["jobs"][0].update(enabled="yes"),
        "a job name with markup": lambda st: st["jobs"][0].update(name="<b>check</b>"),
        "a repo that is no repo": lambda st: st["jobs"][0].update(repo="me/widget\nfixed"),
        "a link elsewhere": lambda st: st["jobs"][0]["recent"][0].update(
            url="https://github.com.example.org/x"),
        "a link with a space": lambda st: st["jobs"][0]["recent"][0].update(
            url="https://github.com/me/widget passed"),
    }

    def bad_status(self, how):
        self.main_status([rec("2026-11-03 06:00", "passed")])
        path = self.root / "homes" / "anvil" / ".local/state/cronward/status.json"
        st = json.loads(path.read_text())
        self.BAD[how](st)
        path.write_text(json.dumps(st))

    def test_watch_says_it_does_not_read_and_keeps_the_good_copy(self):
        self.main_status([rec("2026-11-03 06:00", "failed", "red")])
        self.watch()
        good = notify.copy_path("anvil").read_text()
        for how in self.BAD:
            with self.subTest(how):
                self.bad_status(how)
                err = StringIO()
                with redirect_stderr(err):
                    rc = notify.watch("anvil", config.Config(), now=NOW)
                self.assertEqual(rc, 1)
                self.assertIn("the status on anvil does not read: ", err.getvalue())
                self.assertEqual(notify.copy_path("anvil").read_text(), good)

    def test_status_host_on_a_bad_copy_says_so(self):
        for how in self.BAD:
            with self.subTest(how):
                self.bad_status(how)
                path = self.root / "homes" / "anvil" / ".local/state/cronward/status.json"
                notify.copy_path("anvil").parent.mkdir(parents=True, exist_ok=True)
                notify.copy_path("anvil").write_text(path.read_text())
                out, err = StringIO(), StringIO()
                with redirect_stdout(out), redirect_stderr(err):
                    rc = cli.main(["status", "--host", "anvil", "--json"])
                self.assertEqual((rc, out.getvalue()), (2, ""))
                self.assertIn("does not read", err.getvalue())
                self.assertIn("cronward watch anvil", err.getvalue())
                self.assertNotIn("Traceback", err.getvalue())
