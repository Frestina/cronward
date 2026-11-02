import re
from contextlib import redirect_stdout
from io import StringIO

from cronward import timers
from tests.harness import CronwardTest


def quiet(fn, *args):
    with redirect_stdout(StringIO()) as out:
        result = fn(*args)
    return result, out.getvalue()


class TimersTest(CronwardTest):
    def test_tick_units(self):
        texts = timers.units(timers.TICK)
        service = texts["cronward-tick.service"]
        self.assertTrue(service.startswith(timers.MARK))
        self.assertRegex(service, r"ExecStart=\S+python\S* \S+/bin/cronward tick\n")
        for want in ("Nice=19", "IOSchedulingClass=idle", "MemoryMax=", "TimeoutStartSec="):
            self.assertIn(want, service)
        self.assertIn("OnCalendar=*:01/5", texts["cronward-tick.timer"])
        self.assertIn("Persistent=true", texts["cronward-tick.timer"])

    def test_install_main_and_uninstall(self):
        rc, out = quiet(timers.install)
        self.assertEqual(rc, 0)
        self.assertEqual(sorted(p.name for p in timers.unit_dir().iterdir()),
                         ["cronward-tick.service", "cronward-tick.timer"])
        calls = [c[1:] for c in self.calls("systemctl")]
        self.assertIn(["enable", "--now", "cronward-tick.timer"], calls)
        self.assertEqual(timers.active("cronward-tick"), "active")
        quiet(timers.uninstall)
        self.assertEqual(list(timers.unit_dir().iterdir()), [])

    def test_install_viewer(self):
        quiet(timers.install, "anvil")
        service = (timers.unit_dir() / "cronward-watch.service").read_text()
        self.assertRegex(service, r"cronward watch anvil\n")

    def test_a_host_systemd_would_misread_is_refused(self):
        for host in ("anvil; rm", "-oProxyCommand=x", "a b"):
            with self.subTest(host):
                with self.assertRaises(timers.TimersError):
                    timers.install(host)

    def test_lingering_off_is_said_and_never_changed(self):
        import os
        os.environ["CW_LINGER"] = "no"
        try:
            _, out = quiet(timers.install)
        finally:
            del os.environ["CW_LINGER"]
        self.assertIn("loginctl enable-linger", out)
        self.assertNotIn("enable-linger", str(self.calls("loginctl")))

    def test_a_unit_cronward_did_not_write_is_left(self):
        timers.unit_dir().mkdir(parents=True)
        (timers.unit_dir() / "cronward-tick.timer").write_text("[Timer]\n")
        with self.assertRaises(timers.TimersError):
            timers.install()
        quiet(timers.uninstall)
        self.assertTrue((timers.unit_dir() / "cronward-tick.timer").exists())

    def test_the_viewer_keeps_off_fleets_minutes(self):
        fleet = {0, 15, 45, 50} | set(range(2, 60, 5))
        line = next(s for s in timers.watch_timer("x").schedule if s.startswith("OnCalendar"))
        first, step = map(int, re.search(r":(\d+)/(\d+)", line).groups())
        self.assertFalse(set(range(first, 60, step)) & fleet)
