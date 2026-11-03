"""One tick: claim the due slots, start their runs, follow open runs.

The order is what keeps a slot from running twice: every claim is saved
before any run is started, and the state is saved again after each start.
A claim whose start never finished (the tick died) is found by the next
tick, which looks for a run GitHub may have started for it before calling
it didn't run. See SPEC.md, "A tick".
"""

import datetime
import time
from collections.abc import Callable

from cronward import cron, notify, state, status, tokens
from cronward.config import Config, Job
from cronward.github import Client, GitHubError, HTTPProblem, NoAnswer, NotSent
from cronward.state import DIDNT_RUN, FAILED, PASSED, PENDING
from cronward.status import when

LOOK_FOR = 600  # seconds to look for a run GitHub may have started
FAILED_CONCLUSIONS = ("failure", "timed_out", "startup_failure")

MakeClient = Callable[[str], Client]


def default_client(account: str) -> Client:
    return Client(tokens.load(account))


class Clients:
    """One client per account, made when first needed. A token that cannot be
    loaded is remembered, so every job of that account gets the same reason."""

    def __init__(self, make: MakeClient):
        self._make = make
        self.made: dict[str, Client] = {}
        self._failed: dict[str, str] = {}

    def __call__(self, account: str) -> Client:
        if account in self._failed:
            raise tokens.TokenError(self._failed[account])
        if account not in self.made:
            try:
                self.made[account] = self._make(account)
            except tokens.TokenError as e:
                self._failed[account] = str(e)
                raise
        return self.made[account]


def span(seconds: int) -> str:
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if seconds % size == 0:
            return f"{seconds // size}{unit}"
    return f"{seconds}s"


def explain(e: HTTPProblem, job: Job) -> str:
    """What a refusal means for this job."""
    if e.status == 401:
        return f"GitHub refused the token for {job.account} (expired or revoked?)"
    if e.status == 403 and not e.rate_limited:
        return (f"the token for {job.account} may not do this: it needs Actions: read and "
                f"write on {job.repo} ({e.message})")
    if e.status == 404:
        return (f"{job.repo} or its workflow {job.workflow} was not found, or the token for "
                f"{job.account} cannot see it")
    if e.status == 422 and "workflow_dispatch" in e.message:
        return f"{e.message}: add `workflow_dispatch:` under `on:` in {job.workflow}"
    return str(e)


def _final(rec: dict, kind: str, reason: str, finished: float) -> None:
    rec["kind"] = kind
    rec["reason"] = reason
    rec["finished"] = finished
    rec.pop("looking", None)


def _parse_time(text: object) -> float | None:
    if not isinstance(text, str):
        return None
    try:
        return datetime.datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def claim(cfg: Config, data: dict, now: float) -> tuple[list[tuple[Job, dict]], list[dict]]:
    """Moves each enabled job's `through` to now. Returns the slots to start,
    and the results of missed slots."""
    claimed, missed = [], []
    for job in cfg.jobs:
        js = data["jobs"].setdefault(job.name, {"through": None, "notified": None})
        through = js.get("through")
        # Never backwards: a clock stepped back would claim its slots again.
        js["through"] = now if through is None else max(through, now)
        if through is None or not job.enabled:
            continue
        due = cron.slots(job.schedule, job.zone, through, now)
        if not due:
            continue
        if len(due) > 1:
            n = len(due) - 1
            reason = (f"missed {n} slots, {when(due[0])} to {when(due[-2])}" if n > 1
                      else f"missed the slot at {when(due[0])}")
            rec = state.result(job.name, due[0], DIDNT_RUN,
                               reason + ": the machine was off or cronward was not running",
                               last=due[-2], count=n, finished=now)
            data["results"].append(rec)
            missed.append(rec)
        rec = state.result(job.name, due[-1], claimed=now)
        data["results"].append(rec)
        claimed.append((job, rec))
    return claimed, missed


def start(cfg: Config, job: Job, rec: dict, clients: Clients, data: dict, now: float) -> None:
    """Starts the run for a claimed slot, or makes the result didn't run."""
    try:
        client = clients(job.account)
        wf = client.workflow(job.repo, job.workflow)
        wf_state = wf.get("state") if isinstance(wf, dict) else None
        if wf_state == "disabled_inactivity":
            if not cfg.reenable:
                return _final(rec, DIDNT_RUN, "GitHub turned the workflow off after 60 days "
                                              "without activity in the repo, and reenable "
                                              "is off", now)
            client.enable(job.repo, job.workflow)
            rec["note"] = ("GitHub had turned the workflow off for inactivity; cronward "
                           "turned it back on")
        elif wf_state == "disabled_manually":
            return _final(rec, DIDNT_RUN, "the workflow is turned off in GitHub, and cronward "
                                          "leaves that to you", now)
        elif wf_state != "active":
            return _final(rec, DIDNT_RUN, f"GitHub says the workflow is {wf_state}", now)
        ref = job.ref or (client.repo(job.repo) or {}).get("default_branch")
        if not ref:
            return _final(rec, DIDNT_RUN, f"GitHub gave no default branch for {job.repo}; "
                                          "set ref in the job", now)
    except tokens.TokenError as e:
        return _final(rec, DIDNT_RUN, str(e), now)
    except HTTPProblem as e:
        return _final(rec, DIDNT_RUN, explain(e, job), now)
    except GitHubError as e:
        return _final(rec, DIDNT_RUN, f"{e}; nothing was started", now)

    rec["ref"] = ref
    rec["started"] = now
    try:
        details = client.dispatch(job.repo, job.workflow, ref, job.inputs)
    except NotSent as e:
        return _final(rec, DIDNT_RUN, f"{e}; nothing was started", now)
    except NoAnswer as e:
        details, rec["looking"] = None, str(e)
    except HTTPProblem as e:
        if e.status < 500:
            return _final(rec, DIDNT_RUN, explain(e, job), now)
        details, rec["looking"] = None, str(e)
    if isinstance(details, dict) and isinstance(details.get("workflow_run_id"), int):
        rec["run_id"] = details["workflow_run_id"]
        rec["url"] = details.get("html_url")
        return
    rec.setdefault("looking", "GitHub did not say which run it started")
    _adopt(client, job, rec, data)


def _adopt(client: Client, job: Job, rec: dict, data: dict) -> None:
    """Takes the oldest workflow_dispatch run of the workflow created since the
    start that no other result has."""
    try:
        runs = client.dispatched_runs(job.repo, job.workflow, rec["started"] - 60)
    except GitHubError:
        return
    taken = {r["run_id"] for r in data["results"] if r.get("run_id")}
    fits = [(t, r) for r in runs
            if isinstance(r.get("id"), int) and r["id"] not in taken
            and (t := _parse_time(r.get("created_at"))) is not None and t >= rec["started"] - 5]
    if fits:
        _, run = min(fits, key=lambda f: f[0])
        rec["run_id"] = run["id"]
        rec["url"] = run.get("html_url")
        rec.pop("looking", None)


def follow(job: Job | None, rec: dict, clients: Clients, data: dict, now: float) -> None:
    """Reads a pending run, and makes its result final when it is."""
    if job is None:
        return _final(rec, DIDNT_RUN, "the job was removed from the config before its run "
                                      "finished", now)
    if rec["started"] is None:
        # Claimed by a tick that stopped before it saved the start.
        rec["started"] = rec.get("claimed") or rec["slot"]
        rec["looking"] = "cronward stopped while starting this run"
    try:
        client = clients(job.account)
    except tokens.TokenError as e:
        if now - rec["started"] > job.finish_within:
            _final(rec, DIDNT_RUN, str(e), now)
        return
    if rec["run_id"] is None:
        _adopt(client, job, rec, data)
        if rec["run_id"] is None and now - rec["started"] > LOOK_FOR:
            _final(rec, DIDNT_RUN, f"{rec['looking']}, and no run of it turned up on GitHub",
                   now)
        return
    try:
        run = client.run(job.repo, rec["run_id"])
    except GitHubError as e:
        rec["last_error"] = str(e)
        if now - rec["started"] > job.finish_within:
            _final(rec, DIDNT_RUN, f"not finished after {span(job.finish_within)}, and "
                                   f"cannot read the run: {e}", now)
        return
    rec.pop("last_error", None)
    rec["url"] = run.get("html_url") or rec["url"]
    status = run.get("status")
    if status == "completed":
        c = run.get("conclusion")
        finished = _parse_time(run.get("updated_at")) or now
        if c == "success":
            _final(rec, PASSED, "", finished)
        elif c in FAILED_CONCLUSIONS:
            _final(rec, FAILED, f"the run ended {c}", finished)
        else:
            _final(rec, DIDNT_RUN, f"the run ended {c or 'with no conclusion'}", finished)
    elif now - rec["started"] > job.finish_within:
        _final(rec, DIDNT_RUN, f"not finished after {span(job.finish_within)} "
                               f"(GitHub says {status})", now)


def run(cfg: Config, make: MakeClient = default_client, now: float | None = None,
        manual: Job | None = None) -> list[dict]:
    """One tick, or with manual, one start of that job now. Holds the lock and
    saves as it goes. Returns the notices to give."""
    now = time.time() if now is None else now
    clients = Clients(make)
    with state.locked():
        data = state.load()
        if manual is None:
            claimed, finished = claim(cfg, data, now)
        else:
            rec = state.result(manual.name, now, claimed=now, manual=True)
            data["results"].append(rec)
            claimed, finished = [(manual, rec)], []
        state.save(data)

        for job, rec in claimed:
            start(cfg, job, rec, clients, data, now)
            state.save(data)
            if rec["kind"] != PENDING:
                finished.append(rec)

        if manual is None:
            jobs = {j.name: j for j in cfg.jobs}
            started = {id(rec) for _, rec in claimed}
            for rec in data["results"]:
                if rec["kind"] == PENDING and id(rec) not in started:
                    follow(jobs.get(rec["job"]), rec, clients, data, now)
                    if rec["kind"] != PENDING:
                        finished.append(rec)
            data["last_tick"] = now
            data["tick_error"] = None
            state.error_told_path().unlink(missing_ok=True)

        for account, client in clients.made.items():
            if client.answered:
                data["accounts"].setdefault(account, {})["expires"] = client.expires
        notices = notify.decide(cfg, data, finished, now)
        state.save(data)
        status.write(cfg, data)
    return notices


def wait(cfg: Config, job: Job, slot: float, until: float, make: MakeClient = default_client,
         every: float = 15) -> tuple[dict | None, list[dict]]:
    """Follows the result `cronward run` started for slot until it is final, or
    until `until`. Returns the result as it stands, and the notices to give.
    Takes the lock for each look only, so ticks go on meanwhile; a tick that
    makes the result final gives its notices itself."""
    clients = Clients(make)
    while True:
        now = time.time()
        with state.locked(wait=True):
            data = state.load()
            rec = next((r for r in data["results"]
                        if r["job"] == job.name and r["slot"] == slot), None)
            if rec is None or rec["kind"] != PENDING:
                return rec, []
            follow(job, rec, clients, data, now)
            done = rec["kind"] != PENDING
            notices = notify.decide(cfg, data, [rec], now) if done else []
            state.save(data)
            if done:
                status.write(cfg, data)
                return rec, notices
        if now >= until:
            return rec, []
        time.sleep(every)


def failed(cfg: Config, why: str) -> list[dict]:
    """Records a tick that failed, so status and viewers show it. Returns a
    notice the first time this error is seen since the last tick that worked."""
    try:
        with state.locked(wait=True):
            data = state.load()
            data["tick_error"] = why
            state.save(data)
            status.write(cfg, data)
    except (state.StateError, OSError):
        status_only = state.empty()
        status_only["tick_error"] = why
        state.write_json(state.status_path(), status.build(cfg, status_only))
    told = state.error_told_path()
    try:
        if told.read_text() == why:
            return []
    except OSError:
        pass
    try:
        told.write_text(why)
    except OSError:
        pass
    return [{"title": "cronward: a tick failed", "body": notify.clean(why), "kind": "tick"}]


def dry_run(cfg: Config, now: float | None = None) -> list[str]:
    """What a tick would do now, without saving or starting anything."""
    now = time.time() if now is None else now
    data = state.load()
    lines = []
    for job in cfg.jobs:
        through = data["jobs"].get(job.name, {}).get("through")
        if not job.enabled:
            lines.append(f"{job.name}: disabled")
            continue
        if through is None:
            lines.append(f"{job.name}: new; its first slot is the next one after this tick")
            continue
        due = cron.slots(job.schedule, job.zone, through, now)
        if not due:
            nxt = cron.next_slot(job.schedule, job.zone, now)
            lines.append(f"{job.name}: nothing due; next {when(nxt) if nxt else 'never'}")
            continue
        line = f"{job.name}: would start the slot at {when(due[-1])}"
        if len(due) > 1:
            line += f", and record {len(due) - 1} missed"
        lines.append(line)
    pending = sum(1 for r in data["results"] if r["kind"] == PENDING)
    if pending:
        lines.append(f"would follow {pending} running result{'s' if pending > 1 else ''}")
    return lines

