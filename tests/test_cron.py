import datetime
import unittest
import zoneinfo

from cronward import cron

UTC = datetime.timezone.utc
OSLO = zoneinfo.ZoneInfo("Europe/Oslo")


def t(text, zone=UTC):
    return datetime.datetime.strptime(text, "%Y-%m-%d %H:%M").replace(tzinfo=zone).timestamp()


def walls(ts_list, zone):
    return [datetime.datetime.fromtimestamp(ts, zone).strftime("%m-%d %H:%M %z")
            for ts in ts_list]


class ParseTest(unittest.TestCase):
    def test_fields(self):
        s = cron.parse("*/15 6-8 1,15 JAN-mar mon-fri")
        self.assertEqual(s.minutes, (0, 15, 30, 45))
        self.assertEqual(s.hours, (6, 7, 8))
        self.assertEqual(s.days, {1, 15})
        self.assertEqual(s.months, {1, 2, 3})
        self.assertEqual(s.weekdays, {1, 2, 3, 4, 5})

    def test_sunday_is_0_and_7(self):
        self.assertEqual(cron.parse("0 6 * * 7").weekdays, {0})
        self.assertEqual(cron.parse("0 6 * * 5/1").weekdays, {5, 6, 0})

    def test_either_day_field_matches_when_both_are_set(self):
        s = cron.parse("0 6 1 * MON")
        self.assertTrue(s.on(datetime.date(2026, 11, 1)))  # a Sunday, the 1st
        self.assertTrue(s.on(datetime.date(2026, 11, 2)))  # a Monday
        self.assertFalse(s.on(datetime.date(2026, 11, 3)))
        s = cron.parse("0 6 */2 * MON")  # starts with *: unrestricted, so both must match
        self.assertFalse(s.on(datetime.date(2026, 11, 2)))
        self.assertTrue(s.on(datetime.date(2026, 11, 9)))

    def test_refused(self):
        for bad, why in (("* * * * *", "more often"), ("*/4 * * * *", "more often"),
                         ("58,0 * * * *", "more often"), ("0 0 31 2 *", "never fires"),
                         ("60 * * * *", "outside"), ("0 6 * *", "5"), ("5-1 * * * *", "back"),
                         ("*/0 * * * *", "step"), ("0 6 * * FUNDAY", "not a number")):
            with self.subTest(bad):
                with self.assertRaisesRegex(ValueError, why):
                    cron.parse(bad)

    def test_once_an_hour_across_midnight_is_fine(self):
        cron.parse("58,3 23,0 * * *")  # 23:58 and 00:03 are 5 apart
        cron.parse("55 * * * *")


class SlotsTest(unittest.TestCase):
    def test_slots_after_and_up_to(self):
        s = cron.parse("*/15 * * * *")
        got = cron.slots(s, UTC, t("2026-11-03 06:00"), t("2026-11-03 06:45"))
        self.assertEqual(walls(got, UTC), ["11-03 06:15 +0000", "11-03 06:30 +0000",
                                           "11-03 06:45 +0000"])

    def test_clocks_forward_runs_at_the_first_minute_after(self):
        got = cron.slots(cron.parse("30 2 * * *"), OSLO, t("2027-03-27 12:00", OSLO),
                         t("2027-03-29 12:00", OSLO))
        self.assertEqual(walls(got, OSLO), ["03-28 03:00 +0200", "03-29 02:30 +0200"])

    def test_clocks_back_runs_once(self):
        got = cron.slots(cron.parse("30 2 * * *"), OSLO, t("2026-10-24 12:00", OSLO),
                         t("2026-10-26 12:00", OSLO))
        self.assertEqual(walls(got, OSLO), ["10-25 02:30 +0200", "10-26 02:30 +0100"])

    def test_two_skipped_times_are_one_slot(self):
        got = cron.slots(cron.parse("15,45 2 * * *"), OSLO, t("2027-03-28 00:00", OSLO),
                         t("2027-03-28 12:00", OSLO))
        self.assertEqual(walls(got, OSLO), ["03-28 03:00 +0200"])

    def test_next_slot(self):
        s = cron.parse("0 0 29 2 *")
        nxt = cron.next_slot(s, UTC, t("2026-11-03 00:00"))
        self.assertEqual(walls([nxt], UTC), ["02-29 00:00 +0000"])

    def test_a_long_time_off_lists_only_the_lookback(self):
        s = cron.parse("0 6 * * *")
        got = cron.slots(s, UTC, 0, t("2026-11-03 07:00"))
        self.assertEqual(len(got), cron.LOOKBACK)
