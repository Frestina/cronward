import datetime

from cronward import config
from cronward.config import ConfigError
from tests.harness import JOB, CronwardTest


class ConfigTest(CronwardTest):
    def test_the_template_reads(self):
        cfg = config.parse(config.TEMPLATE)
        self.assertEqual(cfg.accounts, ("personal",))
        self.assertEqual(cfg.jobs, ())
        self.assertTrue(cfg.reenable)
        self.assertEqual(cfg.urgency, "critical")

    def test_the_templates_example_job_reads_when_uncommented(self):
        lines = config.TEMPLATE.splitlines()
        start = lines.index("# [jobs.nightly-check]")
        for i in range(start, start + 11):
            lines[i] = lines[i][2:]
        cfg = config.parse("\n".join(lines))
        job = cfg.job("nightly-check")
        self.assertEqual(job.inputs, {"level": "full"})
        self.assertEqual(job.zone_name, "Europe/Oslo")
        self.assertEqual(job.finish_within, 7200)
        self.assertFalse(job.enabled)

    def test_defaults(self):
        job = config.parse(JOB).job("check")
        self.assertEqual(job.zone, datetime.timezone.utc)
        self.assertEqual(job.finish_within, 6 * 3600)
        self.assertIsNone(job.ref)
        self.assertTrue(job.enabled)

    def test_errors_say_where(self):
        cases = [
            (JOB + "shedule = '0 6 * * *'\n", r"\[jobs.check\]: unknown setting shedule"),
            (JOB.replace('account = "personal"', 'account = "work"'), "add \\[accounts.work\\]"),
            (JOB.replace("me/widget", "widget"), "not owner/name"),
            (JOB.replace("check.yml", "check"), "neither a file name"),
            (JOB.replace("0 6 * * *", "* * * * *"), "more often"),
            (JOB + 'timezone = "Mars/Olympus"\n', "no time zone"),
            (JOB + 'finish_within = "6 hours"\n', "not a time like"),
            (JOB + "enabled = 'no'\n", "enabled must be bool"),
            ("colour = 1\n", "unknown setting colour"),
            ("[notify]\nurgency = 'loud'\n", "urgency must be one of"),
            ("[accounts.personal]\ntoken = 'x'\n", "unknown setting token"),
            ("x = [", "does not read as TOML"),
        ]
        for text, want in cases:
            with self.subTest(want):
                with self.assertRaisesRegex(ConfigError, want):
                    config.parse(text)

    def test_a_token_in_the_config_is_refused(self):
        with self.assertRaises(ConfigError):
            config.parse("[accounts.personal]\ntoken = 'github_pat_x'\n")

    def test_command_as_a_string_or_a_list(self):
        self.assertEqual(config.parse("[notify]\ncommand = \"post 'a b'\"\n").command,
                         ("post", "a b"))
        self.assertEqual(config.parse("[notify]\ncommand = ['post', 'x']\n").command,
                         ("post", "x"))

    def test_local_zone(self):
        zone, name = config.zone("local", "timezone")
        self.assertIsNotNone(datetime.datetime(2026, 1, 1, tzinfo=zone).utcoffset())
        self.assertTrue(name)

    def test_no_config(self):
        with self.assertRaisesRegex(ConfigError, "cronward init"):
            config.load()
        self.assertEqual(config.load(required=False).jobs, ())

    def test_unknown_job(self):
        with self.assertRaisesRegex(ConfigError, "there are: check"):
            config.parse(JOB).job("nope")
