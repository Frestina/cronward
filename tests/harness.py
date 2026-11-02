"""A throwaway home and a fake GitHub for tests: no network, no real home.

CronwardTest gives every test its own HOME with no XDG variables, puts
tests/bin first on the PATH (fakes of notify-send, systemctl, loginctl,
systemd-creds and ssh that record their calls), and a FakeGitHub that a
test passes to cronward as its transport.

    class MyTest(CronwardTest):
        def test_something(self):
            cfg = self.config(JOB)
            self.tick(cfg, at="2026-11-03 06:01")
"""

import datetime
import json
import os
import shutil
import tempfile
import unittest
import urllib.parse
from pathlib import Path
from unittest import mock

from cronward import config, github, tick

BIN = Path(__file__).resolve().parent / "bin"
REPO = "me/widget"
WORKFLOW = "check.yml"
EXPIRES = "2030-01-01 00:00:00 UTC"
JOB = f"""\
[accounts.personal]

[jobs.check]
account = "personal"
repo = "{REPO}"
workflow = "{WORKFLOW}"
schedule = "0 6 * * *"
"""


def at(text: str) -> float:
    """A UTC time like "2026-11-03 06:01" as a timestamp."""
    return datetime.datetime.strptime(text, "%Y-%m-%d %H:%M").replace(
        tzinfo=datetime.timezone.utc).timestamp()


def iso(ts: float) -> str:
    return datetime.datetime.fromtimestamp(ts, datetime.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ")


class FakeGitHub:
    """Answers the calls cronward makes, from a few dicts. `script` is a list
    of (method, path part, answer) that are used up in order before the normal
    answer: answer is a status, "notsent", "noanswer", or "start+<status>",
    which starts the run and then answers with that status."""

    def __init__(self):
        self.clock = 0.0
        self.calls: list[tuple[str, str, dict | None]] = []
        self.repos = {REPO: {"full_name": REPO, "default_branch": "main"}}
        self.workflows = {(REPO, WORKFLOW): {"id": 7, "name": "Check", "state": "active",
                                             "path": f".github/workflows/{WORKFLOW}"}}
        self.runs: dict[int, dict] = {}
        self.next_id = 1000
        self.details = True
        self.no_dispatch: set[tuple[str, str]] = set()
        self.script: list[tuple[str, str, object]] = []
        self.token_ok = True
        self.on_dispatch = None

    def calls_to(self, method: str, part: str = "") -> list:
        return [c for c in self.calls if c[0] == method and part in c[1]]

    def finish(self, run_id: int, conclusion: str) -> None:
        self.runs[run_id].update(status="completed", conclusion=conclusion,
                                 updated_at=iso(self.clock))

    def _start(self, repo: str, wf: str) -> dict:
        self.next_id += 1
        run = {"id": self.next_id, "status": "queued", "conclusion": None,
               "html_url": f"https://github.com/{repo}/actions/runs/{self.next_id}",
               "created_at": iso(self.clock), "updated_at": iso(self.clock),
               "event": "workflow_dispatch", "workflow": wf}
        self.runs[self.next_id] = run
        return run

    def __call__(self, method, path, body, headers):
        data = json.loads(body) if body else None
        self.calls.append((method, path, data))
        for i, (m, part, answer) in enumerate(self.script):
            if m == method and part in path:
                del self.script[i]
                if answer == "notsent":
                    raise github.NotSent("cannot reach api.github.com: refused")
                if answer == "noanswer":
                    raise github.NoAnswer("no answer from api.github.com in 30s")
                if isinstance(answer, str) and answer.startswith("start+"):
                    self._answer(method, path, data)
                    answer = int(answer[6:])
                return answer, {}, b'{"message": "scripted"}'
        return self._answer(method, path, data)

    def _answer(self, method, path, data):
        hdrs = {github.EXPIRY: EXPIRES}
        if not self.token_ok:
            return 401, hdrs, b'{"message": "Bad credentials"}'
        url = urllib.parse.urlsplit(path)
        parts = [urllib.parse.unquote(p) for p in url.path.strip("/").split("/")]

        def ok(obj, status=200):
            return status, hdrs, json.dumps(obj).encode()

        def missing():
            return 404, hdrs, b'{"message": "Not Found"}'

        if parts == ["user"]:
            return ok({"login": "me"})
        if parts == ["user", "repos"]:
            return ok(list(self.repos.values()))
        if parts[0] != "repos":
            return missing()
        repo = f"{parts[1]}/{parts[2]}"
        if repo not in self.repos:
            return missing()
        rest = parts[3:]
        if not rest:
            return ok(self.repos[repo])
        if rest == ["actions", "workflows"]:
            return ok({"workflows": [w for (r, _), w in self.workflows.items() if r == repo]})
        if rest[:2] == ["actions", "runs"]:
            run = self.runs.get(int(rest[2]))
            return ok(run) if run else missing()
        wf = self.workflows.get((repo, rest[2]))
        if wf is None:
            return missing()
        if len(rest) == 3:
            return ok(wf)
        if rest[3] == "enable" and method == "PUT":
            wf["state"] = "active"
            return 204, hdrs, b""
        if rest[3] == "dispatches" and method == "POST":
            if (repo, rest[2]) in self.no_dispatch:
                return 422, hdrs, b'{"message": "Workflow does not have \'workflow_dispatch\' trigger"}'
            if self.on_dispatch:
                self.on_dispatch()
            run = self._start(repo, rest[2])
            if not self.details:
                return 204, hdrs, b""
            return ok({"workflow_run_id": run["id"], "html_url": run["html_url"],
                       "run_url": f"https://api.github.com/repos/{repo}/actions/runs/{run['id']}"})
        if rest[3] == "runs":
            runs = [r for r in self.runs.values() if r["workflow"] == rest[2]]
            return ok({"workflow_runs": sorted(runs, key=lambda r: -r["id"])})
        return missing()


class CronwardTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="cronward-test-"))
        (self.root / "calls").mkdir()
        (self.root / "homes").mkdir()
        self.home = self.root / "home"
        self.home.mkdir()
        env = {"HOME": str(self.home), "CW_FAKE": str(self.root),
               "PATH": f"{BIN}{os.pathsep}{os.environ.get('PATH', '')}"}
        patch = mock.patch.dict(os.environ, env)
        patch.start()
        self.addCleanup(patch.stop)
        for key in [k for k in os.environ if k.startswith("XDG_") and k.endswith("_HOME")]:
            del os.environ[key]
        self.addCleanup(shutil.rmtree, self.root)
        self.gh = FakeGitHub()
        sleep = mock.patch.object(github.Client, "__init__", _no_sleep_init(self.gh))
        sleep.start()
        self.addCleanup(sleep.stop)

    def config(self, text: str = JOB) -> config.Config:
        path = config.config_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        return config.load()

    def make(self, account: str) -> github.Client:
        return github.Client("t0ken", self.gh)

    def tick(self, cfg: config.Config, when: str | float, **kw) -> list[dict]:
        now = at(when) if isinstance(when, str) else when
        self.gh.clock = now
        return tick.run(cfg, self.make, now=now, **kw)

    def calls(self, name: str) -> list[list[str]]:
        out = []
        for p in sorted((self.root / "calls").glob(f"{name}.*"), key=os.path.getmtime):
            out.append(p.read_bytes().decode().split("\0")[:-1])
        return out


def _no_sleep_init(gh: FakeGitHub):
    """Client.__init__ with sleeps that do nothing, and the fake as the
    transport whenever cronward makes a client itself."""
    real = github.Client.__init__

    def init(self, token, transport=None, sleep=None):
        real(self, token, transport or gh, sleep=lambda s: None)
    return init
