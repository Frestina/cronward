import json

from cronward import state, tick
from tests.harness import JOB, REPO, CronwardTest, at


def results(job="check"):
    return [r for r in state.load()["results"] if r["job"] == job]


class StartTest(CronwardTest):
    def test_a_new_job_starts_from_the_first_tick_and_runs_at_its_next_slot(self):
        cfg = self.config()
        self.tick(cfg, "2026-11-03 05:56")
        self.assertEqual(self.gh.calls_to("POST"), [])
        self.tick(cfg, "2026-11-03 06:01")
        posts = self.gh.calls_to("POST", "/dispatches")
        self.assertEqual(len(posts), 1)
        self.assertEqual(posts[0][2], {"ref": "main", "inputs": {}, "return_run_details": True})
        [rec] = results()
        self.assertEqual(rec["kind"], state.PENDING)
        self.assertEqual(rec["slot"], at("2026-11-03 06:00"))
        self.assertEqual(rec["run_id"], 1001)
        self.assertEqual(rec["url"], f"https://github.com/{REPO}/actions/runs/1001")

    def test_a_job_added_at_10_does_not_run_its_6_oclock_slot(self):
        cfg = self.config()
        self.tick(cfg, "2026-11-03 10:01")
        self.tick(cfg, "2026-11-03 10:06")
        self.assertEqual(self.gh.calls_to("POST"), [])

    def test_a_slot_never_runs_twice(self):
        cfg = self.config()
        self.tick(cfg, "2026-11-03 05:56")
        self.tick(cfg, "2026-11-03 06:01")
        self.tick(cfg, "2026-11-03 06:01")
        self.tick(cfg, "2026-11-03 06:06")
        self.assertEqual(len(self.gh.calls_to("POST", "/dispatches")), 1)

    def test_the_claim_is_saved_before_the_run_is_started(self):
        cfg = self.config()
        self.tick(cfg, "2026-11-03 05:56")
        seen = []
        self.gh.on_dispatch = lambda: seen.append(
            (json.loads(state.path().read_text())["jobs"]["check"]["through"],
             [r["kind"] for r in json.loads(state.path().read_text())["results"]]))
        self.tick(cfg, "2026-11-03 06:01")
        self.assertEqual(seen, [(at("2026-11-03 06:01"), [state.PENDING])])

    def test_missed_slots_are_one_result_and_only_the_latest_runs(self):
        cfg = self.config()
        self.tick(cfg, "2026-11-01 05:56")
        self.tick(cfg, "2026-11-04 09:00")  # off for four mornings
        self.assertEqual(len(self.gh.calls_to("POST", "/dispatches")), 1)
        missed, ran = results()
        self.assertEqual(missed["kind"], state.DIDNT_RUN)
        self.assertEqual(missed["count"], 3)
        self.assertIn("missed 3 slots", missed["reason"])
        self.assertIn("machine was off", missed["reason"])
        self.assertEqual(ran["slot"], at("2026-11-04 06:00"))

    def test_a_disabled_job_reports_nothing_missed_when_turned_on_again(self):
        self.config()
        self.tick(self.config(), "2026-11-01 05:56")
        off = self.config(JOB + "enabled = false\n")
        self.tick(off, "2026-11-02 06:01")
        self.tick(off, "2026-11-03 06:01")
        self.tick(self.config(), "2026-11-03 07:01")
        self.assertEqual(results(), [])
        self.assertEqual(self.gh.calls_to("POST"), [])

    def test_inputs_and_ref_are_sent(self):
        cfg = self.config(JOB + 'ref = "release"\ninputs = { level = "full", deep = true }\n')
        self.tick(cfg, "2026-11-03 05:56")
        self.tick(cfg, "2026-11-03 06:01")
        body = self.gh.calls_to("POST", "/dispatches")[0][2]
        self.assertEqual(body["ref"], "release")
        self.assertEqual(body["inputs"], {"level": "full", "deep": "true"})
        self.assertNotIn(("GET", f"/repos/{REPO}", None), self.gh.calls)  # no default branch needed


class WorkflowStateTest(CronwardTest):
    def ready(self, extra=""):
        cfg = self.config(extra + JOB)
        self.tick(cfg, "2026-11-03 05:56")
        return cfg

    def test_a_workflow_github_turned_off_is_turned_on_and_told(self):
        cfg = self.ready()
        self.gh.workflows[(REPO, "check.yml")]["state"] = "disabled_inactivity"
        notices = self.tick(cfg, "2026-11-03 06:01")
        self.assertEqual(len(self.gh.calls_to("PUT", "/enable")), 1)
        self.assertEqual(len(self.gh.calls_to("POST", "/dispatches")), 1)
        self.assertEqual([n["kind"] for n in notices], [])  # told when the run ends
        self.gh.finish(1001, "success")
        notices = self.tick(cfg, "2026-11-03 06:11")
        self.assertIn("turned back on", notices[0]["title"])

    def test_with_reenable_off_it_is_only_told(self):
        cfg = self.ready("reenable = false\n")
        self.gh.workflows[(REPO, "check.yml")]["state"] = "disabled_inactivity"
        notices = self.tick(cfg, "2026-11-03 06:01")
        self.assertEqual(self.gh.calls_to("PUT"), [])
        self.assertEqual(self.gh.calls_to("POST"), [])
        self.assertEqual(results()[0]["kind"], state.DIDNT_RUN)
        self.assertIn("reenable is off", notices[0]["body"])

    def test_a_workflow_the_user_turned_off_is_left_alone(self):
        cfg = self.ready()
        self.gh.workflows[(REPO, "check.yml")]["state"] = "disabled_manually"
        self.tick(cfg, "2026-11-03 06:01")
        self.assertEqual(self.gh.calls_to("PUT"), [])
        self.assertEqual(self.gh.calls_to("POST"), [])
        self.assertIn("turned off in GitHub", results()[0]["reason"])

    def test_refusals_say_what_they_mean(self):
        cases = [
            (lambda: self.gh.no_dispatch.add((REPO, "check.yml")), "add `workflow_dispatch:`"),
            (lambda: self.gh.workflows.clear(), "was not found"),
            (lambda: setattr(self.gh, "token_ok", False), "refused the token for personal"),
        ]
        for i, (breakit, want) in enumerate(cases):
            with self.subTest(want):
                state.path().unlink(missing_ok=True)
                cfg = self.ready()
                breakit()
                self.tick(cfg, f"2026-11-0{3 + i} 06:01")
                rec = results()[-1]
                self.assertEqual(rec["kind"], state.DIDNT_RUN)
                self.assertIn(want, rec["reason"])

    def test_no_token_is_didnt_run(self):
        cfg = self.config()
        tick.run(cfg, now=at("2026-11-03 05:56"))
        tick.run(cfg, now=at("2026-11-03 06:01"))
        rec = results()[0]
        self.assertEqual(rec["kind"], state.DIDNT_RUN)
        self.assertIn("cronward token set personal", rec["reason"])


class RetryTest(CronwardTest):
    def ready(self):
        cfg = self.config()
        self.tick(cfg, "2026-11-03 05:56")
        return cfg

    def test_a_connection_that_never_opened_is_tried_again(self):
        cfg = self.ready()
        self.gh.script = [("POST", "/dispatches", "notsent"), ("POST", "/dispatches", 429)]
        self.tick(cfg, "2026-11-03 06:01")
        self.assertEqual(len(self.gh.calls_to("POST")), 3)
        self.assertEqual(len(self.gh.runs), 1)
        self.assertEqual(results()[0]["run_id"], 1001)

    def test_a_5xx_is_never_sent_again_and_the_run_it_started_is_adopted(self):
        cfg = self.ready()
        self.gh.script = [("POST", "/dispatches", "start+502")]
        self.tick(cfg, "2026-11-03 06:01")
        self.assertEqual(len(self.gh.calls_to("POST")), 1)
        rec = results()[0]
        self.assertEqual(rec["run_id"], 1001)
        self.assertNotIn("looking", rec)

    def test_a_5xx_with_no_run_is_looked_for_then_didnt_run(self):
        cfg = self.ready()
        self.gh.script = [("POST", "/dispatches", 502)]
        self.tick(cfg, "2026-11-03 06:01")
        self.assertEqual(results()[0]["kind"], state.PENDING)
        self.tick(cfg, "2026-11-03 06:06")
        self.assertEqual(results()[0]["kind"], state.PENDING)
        self.tick(cfg, "2026-11-03 06:16")
        rec = results()[0]
        self.assertEqual(rec["kind"], state.DIDNT_RUN)
        self.assertIn("no run of it turned up", rec["reason"])
        self.assertEqual(len(self.gh.calls_to("POST")), 1)

    def test_a_lost_answer_is_not_sent_again(self):
        cfg = self.ready()
        self.gh.script = [("POST", "/dispatches", "noanswer")]
        self.tick(cfg, "2026-11-03 06:01")
        self.assertEqual(len(self.gh.calls_to("POST")), 1)

    def test_no_run_details_finds_the_run(self):
        cfg = self.ready()
        self.gh.details = False
        self.tick(cfg, "2026-11-03 06:01")
        self.assertEqual(results()[0]["run_id"], 1001)

    def test_a_claim_whose_tick_died_finds_its_run_or_says_so(self):
        cfg = self.ready()
        with state.locked():
            data = state.load()
            data["jobs"]["check"]["through"] = at("2026-11-03 06:01")
            data["results"].append(state.result("check", at("2026-11-03 06:00"),
                                                claimed=at("2026-11-03 06:01")))
            state.save(data)
        self.tick(cfg, "2026-11-03 06:06")
        self.assertEqual(results()[0]["kind"], state.PENDING)
        self.tick(cfg, "2026-11-03 06:16")
        rec = results()[0]
        self.assertEqual(rec["kind"], state.DIDNT_RUN)
        self.assertIn("cronward stopped while starting", rec["reason"])
        self.assertEqual(self.gh.calls_to("POST"), [])


class FollowTest(CronwardTest):
    def started(self, extra=""):
        cfg = self.config(JOB + extra)
        self.tick(cfg, "2026-11-03 05:56")
        self.tick(cfg, "2026-11-03 06:01")
        return cfg

    def test_conclusions(self):
        for conclusion, kind in (("success", state.PASSED), ("failure", state.FAILED),
                                 ("timed_out", state.FAILED), ("cancelled", state.DIDNT_RUN),
                                 ("skipped", state.DIDNT_RUN), ("neutral", state.DIDNT_RUN)):
            with self.subTest(conclusion):
                state.path().unlink(missing_ok=True)
                self.gh.runs.clear()
                cfg = self.started()
                run_id = results()[-1]["run_id"]
                self.tick(cfg, "2026-11-03 06:06")
                self.assertEqual(results()[-1]["kind"], state.PENDING)
                self.gh.finish(run_id, conclusion)
                self.tick(cfg, "2026-11-03 06:11")
                self.assertEqual(results()[-1]["kind"], kind)

    def test_a_run_not_finished_in_time_didnt_run(self):
        cfg = self.started('finish_within = "1h"\n')
        self.tick(cfg, "2026-11-03 06:56")
        self.assertEqual(results()[0]["kind"], state.PENDING)
        self.tick(cfg, "2026-11-03 07:06")
        rec = results()[0]
        self.assertEqual(rec["kind"], state.DIDNT_RUN)
        self.assertIn("not finished after 1h (GitHub says queued)", rec["reason"])

    def test_a_failed_read_keeps_it_pending(self):
        cfg = self.started()
        self.gh.script = [("GET", "/runs/", 502)] * 3
        self.tick(cfg, "2026-11-03 06:06")
        self.assertEqual(results()[0]["kind"], state.PENDING)

    def test_a_removed_job_does_not_stay_pending(self):
        self.started()
        cfg = self.config("[accounts.personal]\n")
        self.tick(cfg, "2026-11-03 06:06")
        self.assertIn("removed from the config", results()[0]["reason"])


class NoticeTest(CronwardTest):
    def day(self, cfg, day, conclusion):
        self.tick(cfg, f"2026-11-{day:02} 06:01")
        run_id = results()[-1]["run_id"]
        self.gh.finish(run_id, conclusion)
        return self.tick(cfg, f"2026-11-{day:02} 06:11")

    def test_notices_follow_changes(self):
        cfg = self.config()
        self.tick(cfg, "2026-11-03 05:56")
        self.assertEqual(self.day(cfg, 3, "success"), [])
        [n] = self.day(cfg, 4, "failure")
        self.assertEqual(n["title"], "cronward: check failed")
        self.assertIn(REPO, n["body"])
        self.assertIn("https://github.com/", n["body"])
        self.assertEqual(self.day(cfg, 5, "failure"), [])
        [n] = self.day(cfg, 6, "success")
        self.assertEqual(n["title"], "cronward: check passes again")

    def test_a_first_result_that_is_not_passed_is_told(self):
        cfg = self.config()
        self.tick(cfg, "2026-11-03 05:56")
        [n] = self.day(cfg, 3, "cancelled")
        self.assertEqual(n["title"], "cronward: check didn't run")

    def test_an_older_run_failing_after_a_newer_passed_is_told(self):
        cfg = self.config(JOB.replace("0 6 * * *", "*/15 * * * *"))
        self.tick(cfg, "2026-11-03 05:56")
        self.tick(cfg, "2026-11-03 06:01")
        self.tick(cfg, "2026-11-03 06:16")
        first, second = results()
        self.gh.finish(second["run_id"], "success")
        self.tick(cfg, "2026-11-03 06:21")
        self.gh.finish(first["run_id"], "failure")
        [n] = self.tick(cfg, "2026-11-03 06:26")
        self.assertEqual(n["kind"], state.FAILED)

    def test_a_token_about_to_expire_is_told_once_a_day(self):
        cfg = self.config()
        soon = "2026-11-10 00:00:00 UTC"
        self.tick(cfg, "2026-11-03 04:56")
        with state.locked():
            data = state.load()
            data["accounts"]["personal"] = {"expires": soon}
            state.save(data)
        # No slot is due, so no client is made and the stored expiry stands.
        tick_notices = tick.run(cfg, lambda a: _NoCall(), now=at("2026-11-03 05:00"))
        self.assertEqual([n["kind"] for n in tick_notices], ["token"])
        again = tick.run(cfg, lambda a: _NoCall(), now=at("2026-11-03 05:30"))
        self.assertEqual(again, [])

    def test_status_json_is_written_every_tick(self):
        cfg = self.config()
        self.tick(cfg, "2026-11-03 05:56")
        st = json.loads(state.status_path().read_text())
        self.assertEqual(st["last_tick"], at("2026-11-03 05:56"))
        self.assertEqual(st["jobs"][0]["name"], "check")


class _NoCall:
    expires = None


class LockTest(CronwardTest):
    def test_a_second_tick_waits_for_nothing(self):
        cfg = self.config()
        with state.locked():
            with self.assertRaises(state.Busy):
                self.tick(cfg, "2026-11-03 06:01")


class DryRunTest(CronwardTest):
    def test_dry_run_changes_nothing(self):
        cfg = self.config()
        self.tick(cfg, "2026-11-03 05:56")
        before = state.path().read_text()
        lines = tick.dry_run(cfg, now=at("2026-11-03 06:01"))
        self.assertEqual(lines, ["check: would start the slot at "
                                 + tick.when(at("2026-11-03 06:00"))])
        self.assertEqual(state.path().read_text(), before)
        self.assertEqual(self.gh.calls_to("POST"), [])
