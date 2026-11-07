import importlib.machinery
import importlib.util
import io
import json
import subprocess
from contextlib import redirect_stderr
from pathlib import Path
from unittest import mock

from cronward import notify, state
from tests.harness import REPO, WORKFLOW, CronwardTest, at

NTFY = Path(__file__).resolve().parent.parent / "examples" / "notify-ntfy"


def load_ntfy():
    loader = importlib.machinery.SourceFileLoader("notify_ntfy", str(NTFY))
    spec = importlib.util.spec_from_loader("notify_ntfy", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


ntfy = load_ntfy()
URL = "https://github.com/me/widget/actions/runs/7"


def failed_notice() -> dict:
    """A notice as cronward gives it to notify.command."""
    rec = {**state.result("check", at("2026-11-03 06:00"), state.FAILED,
                          "the run ended failure"), "url": URL}
    return notify._result_notice(rec, {"repo": REPO, "workflow": WORKFLOW}, state.PASSED)


class _Response:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return b"{}"


class _Opener:
    def __init__(self):
        self.requests = []

    def open(self, req, timeout):
        self.requests.append(req)
        return _Response()


class NtfyTest(CronwardTest):
    def env(self, text: str, mode: int = 0o600) -> Path:
        path = ntfy.default_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        path.chmod(mode)
        return path

    def problem(self, text: str, mode: int = 0o600) -> str:
        with self.assertRaises(ntfy.Problem) as cm:
            ntfy.settings(self.env(text, mode))
        return str(cm.exception)

    def test_settings_from_the_file_next_to_the_config(self):
        self.assertEqual(ntfy.default_path(), Path.home() / ".config/cronward/ntfy.env")
        path = self.env("# for cronward\nNTFY_TOPIC = \"cronward-k3v9\"\n\n")
        self.assertEqual(ntfy.settings(path),
                         {"NTFY_TOPIC": "cronward-k3v9", "NTFY_SERVER": "https://ntfy.sh"})
        path = self.env("NTFY_TOPIC=t\nNTFY_SERVER=https://ntfy.example.org/\nNTFY_TOKEN=tk_x\n")
        self.assertEqual(ntfy.settings(path), {"NTFY_TOPIC": "t", "NTFY_TOKEN": "tk_x",
                                               "NTFY_SERVER": "https://ntfy.example.org"})

    def test_settings_that_do_not_do(self):
        self.assertIn("chmod 600", self.problem("NTFY_TOPIC=t\n", 0o644))
        self.assertIn("has no NTFY_TOPIC", self.problem("NTFY_SERVER=https://ntfy.sh\n"))
        self.assertIn("line 2: not one of", self.problem("NTFY_TOPIC=t\nTOPIC=t\n"))
        self.assertIn("only sent over https",
                      self.problem("NTFY_TOPIC=t\nNTFY_SERVER=http://ntfy.example.org\n"
                                   "NTFY_TOKEN=tk_x\n"))
        self.assertIn("not an http or https",
                      self.problem("NTFY_TOPIC=t\nNTFY_SERVER=ftp://ntfy.example.org\n"))
        ntfy.default_path().unlink()
        with self.assertRaises(ntfy.Problem) as cm:
            ntfy.settings(ntfy.default_path())
        self.assertIn("write it with NTFY_TOPIC=", str(cm.exception))

    def test_message_from_a_cronward_notice(self):
        msg = ntfy.message(failed_notice(), "t")
        self.assertEqual(msg["topic"], "t")
        self.assertEqual(msg["title"], "cronward: check failed")
        self.assertIn("me/widget check.yml", msg["message"])
        self.assertEqual((msg["priority"], msg["click"]), (4, URL))
        passed = {**failed_notice(), "kind": state.PASSED, "title": "cronward: check passes again"}
        self.assertEqual(ntfy.message(passed, "t")["priority"], 3)
        token = {"title": "cronward: the token for personal expires soon", "body": "...",
                 "kind": "token", "account": "personal"}
        self.assertNotIn("click", ntfy.message(token, "t"))
        self.assertNotIn("click", ntfy.message({**failed_notice(),
                                                "url": "https://example.org/x"}, "t"))
        with self.assertRaises(ntfy.Problem):
            ntfy.message([], "t")

    def test_send_posts_json_with_the_token_and_follows_no_redirect(self):
        opener = _Opener()
        cfg = {"NTFY_SERVER": "https://ntfy.example.org", "NTFY_TOPIC": "t",
               "NTFY_TOKEN": "tk_x"}
        ntfy.send(cfg, {"topic": "t", "title": "x"}, opener)
        req = opener.requests[0]
        self.assertEqual((req.full_url, req.get_method()), ("https://ntfy.example.org/", "POST"))
        self.assertEqual(req.get_header("Authorization"), "Bearer tk_x")
        self.assertEqual(json.loads(req.data), {"topic": "t", "title": "x"})
        ntfy.send({**cfg, "NTFY_TOKEN": ""}, {}, opener)
        self.assertIsNone(opener.requests[1].get_header("Authorization"))
        self.assertIsNone(ntfy._NoRedirect().redirect_request(req, None, 302, "Found", {},
                                                              "https://example.org/"))

    def test_main_sends_what_is_on_stdin(self):
        self.env("NTFY_TOPIC=t\n")
        with mock.patch.object(ntfy, "send") as send:
            rc = ntfy.main([], io.StringIO(json.dumps(failed_notice())))
        self.assertEqual(rc, 0)
        cfg, msg = send.call_args.args
        self.assertEqual((cfg["NTFY_SERVER"], msg["topic"]), ("https://ntfy.sh", "t"))
        err = io.StringIO()
        with redirect_stderr(err):
            self.assertEqual(ntfy.main([], io.StringIO("not json")), 2)
        self.assertIn("not JSON", err.getvalue())

    def test_cronward_runs_it_and_says_when_the_server_is_not_there(self):
        # Port 9 on this machine, so nothing leaves it.
        path = self.env("NTFY_TOPIC=t\nNTFY_SERVER=http://127.0.0.1:9\n")
        done = subprocess.run([str(NTFY), str(path)], input=json.dumps(failed_notice()).encode(),
                              capture_output=True, timeout=30)
        self.assertEqual(done.returncode, 1, done.stderr)
        self.assertIn(b"notify-ntfy: cannot reach http://127.0.0.1:9", done.stderr)
