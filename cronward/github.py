"""The only code that talks to GitHub, and only to api.github.com.

The address is fixed here, and a redirect to any other host is refused.
Tests replace the transport, never the address.

Errors say whether the request may have reached GitHub, because a start
that may have happened must never be sent again:

- NotSent: the connection never opened, so it is safe to send again.
- NoAnswer: sent, but no answer came back; GitHub may have acted on it.
- HTTPProblem: GitHub answered with an error status.
"""

import http.client
import json
import socket
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable

from cronward import __version__

API = "https://api.github.com"
HOST = "api.github.com"
VERSION = "2022-11-28"
TIMEOUT = 30
LIMIT = 8 * 1024 * 1024
EXPIRY = "github-authentication-token-expiration"

# method, path, body, headers -> status, lower-cased headers, body
Transport = Callable[[str, str, bytes | None, dict[str, str]], tuple[int, dict[str, str], bytes]]


class GitHubError(Exception):
    pass


class NotSent(GitHubError):
    """The connection never opened."""


class NoAnswer(GitHubError):
    """The request may have reached GitHub, but no answer came back."""


class HTTPProblem(GitHubError):
    def __init__(self, status: int, message: str, headers: dict[str, str]):
        super().__init__(f"GitHub answered {status}: {message}")
        self.status = status
        self.message = message
        self.headers = headers

    @property
    def rate_limited(self) -> bool:
        return self.status == 429 or (self.status == 403 and (
            self.headers.get("x-ratelimit-remaining") == "0" or "rate limit" in
            self.message.lower()))


class _SameHost(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if urllib.parse.urlsplit(newurl).hostname != HOST:
            raise urllib.error.HTTPError(newurl, code, f"refused redirect to {newurl}",
                                         headers, fp)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_opener = urllib.request.build_opener(_SameHost())


def urllib_transport(method: str, path: str, body: bytes | None,
                     headers: dict[str, str]) -> tuple[int, dict[str, str], bytes]:
    if not path.startswith("/"):
        raise ValueError(f"not a path on {HOST}: {path!r}")
    req = urllib.request.Request(API + path, data=body, method=method, headers=headers)
    try:
        with _opener.open(req, timeout=TIMEOUT) as r:
            return r.status, {k.lower(): v for k, v in r.headers.items()}, r.read(LIMIT)
    except urllib.error.HTTPError as e:
        hdrs = {k.lower(): v for k, v in (e.headers or {}).items()}
        return e.code, hdrs, e.read(LIMIT) if e.fp else b""
    except urllib.error.URLError as e:
        reason = e.reason
        if isinstance(reason, (socket.gaierror, ConnectionRefusedError, ssl.SSLError)) or (
                isinstance(reason, OSError) and reason.errno in (101, 113)):  # unreachable
            raise NotSent(f"cannot reach {HOST}: {reason}") from None
        raise NoAnswer(f"no answer from {HOST}: {reason}") from None
    except (TimeoutError, socket.timeout):
        raise NoAnswer(f"no answer from {HOST} in {TIMEOUT}s") from None
    except (ConnectionError, OSError, http.client.HTTPException) as e:
        raise NoAnswer(f"the connection to {HOST} broke: {e}") from None


def q(part: str) -> str:
    return urllib.parse.quote(part, safe="")


def repo_path(repo: str) -> str:
    owner, name = repo.split("/", 1)
    return f"/repos/{q(owner)}/{q(name)}"


class Client:
    def __init__(self, token: str, transport: Transport | None = None,
                 sleep: Callable[[float], None] = time.sleep):
        self._token = token
        self._transport = transport or urllib_transport
        self.sleep = sleep
        self.expires: str | None = None  # as GitHub sends it, "2026-12-01 00:00:00 UTC"
        self.answered = False  # a call succeeded, so expires None means none

    def call(self, method: str, path: str, body: dict | None = None):
        headers = {
            "Authorization": f"Bearer {self._token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": VERSION,
            "User-Agent": f"cronward/{__version__}",
        }
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        status, hdrs, raw = self._transport(method, path, data, headers)
        if status < 400:
            self.answered = True
            self.expires = hdrs.get(EXPIRY) or None
        try:
            parsed = json.loads(raw) if raw.strip() else None
        except ValueError:
            parsed = None
        if status >= 400:
            message = parsed.get("message") if isinstance(parsed, dict) else None
            raise HTTPProblem(status, str(message or raw[:200].decode(errors="replace")
                                          or "no message"), hdrs)
        return status, parsed

    def get(self, path: str, waits: tuple[float, ...] = (5, 10)):
        """A read, retried on errors that may pass: reading twice is harmless."""
        for wait in (*waits, None):
            try:
                return self.call("GET", path)[1]
            except (NotSent, NoAnswer):
                if wait is None:
                    raise
            except HTTPProblem as e:
                if wait is None or not (e.status >= 500 or e.status == 408 or e.rate_limited):
                    raise
            self.sleep(wait)
        raise AssertionError("unreachable")

    def user(self) -> dict:
        return self.get("/user")

    def repo(self, repo: str) -> dict:
        return self.get(repo_path(repo))

    def workflow(self, repo: str, workflow: str) -> dict:
        return self.get(f"{repo_path(repo)}/actions/workflows/{q(workflow)}")

    def enable(self, repo: str, workflow: str) -> None:
        self.call("PUT", f"{repo_path(repo)}/actions/workflows/{q(workflow)}/enable")

    def dispatch(self, repo: str, workflow: str, ref: str, inputs: dict[str, str],
                 waits: tuple[float, ...] = (5, 10, 20)) -> dict | None:
        """Starts the workflow. Retried only when GitHub cannot have acted on
        the request: a connection that never opened, 408, 429 or a rate limit.
        Returns the run's details, or None when GitHub sent none."""
        body = {"ref": ref, "inputs": inputs, "return_run_details": True}
        path = f"{repo_path(repo)}/actions/workflows/{q(workflow)}/dispatches"
        for wait in (*waits, None):
            try:
                status, data = self.call("POST", path, body)
                return data if status == 200 and isinstance(data, dict) else None
            except NotSent:
                if wait is None:
                    raise
            except HTTPProblem as e:
                if wait is None or not (e.status == 408 or e.rate_limited):
                    raise
            self.sleep(wait)
        raise AssertionError("unreachable")

    def run(self, repo: str, run_id: int) -> dict:
        return self.get(f"{repo_path(repo)}/actions/runs/{int(run_id)}")

    def dispatched_runs(self, repo: str, workflow: str, since: float) -> list[dict]:
        """The workflow's workflow_dispatch runs created since a time, newest first."""
        when = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(since))
        data = self.get(f"{repo_path(repo)}/actions/workflows/{q(workflow)}/runs"
                        f"?event=workflow_dispatch&per_page=20&created=%3E%3D{when}")
        runs = data.get("workflow_runs", []) if isinstance(data, dict) else []
        return [r for r in runs if isinstance(r, dict)]

    def repos(self) -> list[dict]:
        out: list[dict] = []
        for page in range(1, 11):
            data = self.get(f"/user/repos?per_page=100&page={page}&sort=full_name")
            if not isinstance(data, list) or not data:
                break
            out.extend(r for r in data if isinstance(r, dict))
            if len(data) < 100:
                break
        return out

    def workflows(self, repo: str) -> list[dict]:
        data = self.get(f"{repo_path(repo)}/actions/workflows?per_page=100")
        found = data.get("workflows", []) if isinstance(data, dict) else []
        return [w for w in found if isinstance(w, dict)]
