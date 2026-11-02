import email.message
import unittest
import urllib.error
import urllib.request

from cronward import github
from tests.harness import EXPIRES, REPO, FakeGitHub


class ClientTest(unittest.TestCase):
    def setUp(self):
        self.gh = FakeGitHub()
        self.client = github.Client("t0ken", self.gh, sleep=lambda s: None)

    def test_headers_and_expiry(self):
        seen = []

        def transport(method, path, body, headers):
            seen.append(headers)
            return 200, {github.EXPIRY: EXPIRES}, b'{"login": "me"}'
        client = github.Client("t0ken", transport)
        self.assertEqual(client.user()["login"], "me")
        self.assertEqual(seen[0]["Authorization"], "Bearer t0ken")
        self.assertEqual(seen[0]["X-GitHub-Api-Version"], github.VERSION)
        self.assertEqual(client.expires, EXPIRES)

    def test_reads_are_retried_on_5xx(self):
        self.gh.script = [("GET", "/user", 502), ("GET", "/user", "noanswer")]
        self.assertEqual(self.client.user()["login"], "me")
        self.assertEqual(len(self.gh.calls), 3)

    def test_reads_are_not_retried_on_404(self):
        self.gh.repos.clear()
        with self.assertRaises(github.HTTPProblem) as e:
            self.client.repo(REPO)
        self.assertEqual(e.exception.status, 404)
        self.assertEqual(len(self.gh.calls), 1)

    def test_dispatch_is_retried_only_where_github_cannot_have_acted(self):
        for answer, tries in (("notsent", 2), (429, 2), (408, 2), (500, 1), ("noanswer", 1)):
            with self.subTest(answer):
                self.gh.calls.clear()
                self.gh.script = [("POST", "/dispatches", answer)]
                try:
                    self.client.dispatch(REPO, "check.yml", "main", {})
                except github.GitHubError:
                    pass
                self.assertEqual(len(self.gh.calls_to("POST")), tries)

    def test_paths_are_quoted(self):
        with self.assertRaises(github.HTTPProblem):
            self.client.call("GET", github.repo_path("me/we ird") + "/x")
        self.assertEqual(self.gh.calls[0][1], "/repos/me/we%20ird/x")

    def test_rate_limit(self):
        e = github.HTTPProblem(403, "API rate limit exceeded", {})
        self.assertTrue(e.rate_limited)
        self.assertFalse(github.HTTPProblem(403, "Resource not accessible", {}).rate_limited)


class HostTest(unittest.TestCase):
    def test_only_paths_on_api_github_com(self):
        with self.assertRaises(ValueError):
            github.urllib_transport("GET", "https://example.com/x", None, {})

    def test_redirects_elsewhere_are_refused(self):
        handler = github._SameHost()
        req = urllib.request.Request("https://api.github.com/repos/me/old")
        hdrs = email.message.Message()
        with self.assertRaises(urllib.error.HTTPError) as refused:
            handler.redirect_request(req, None, 301, "Moved", hdrs, "https://evil.example/x")
        refused.exception.close()
        same = handler.redirect_request(req, None, 301, "Moved", hdrs,
                                        "https://api.github.com/repositories/1")
        self.assertEqual(same.full_url, "https://api.github.com/repositories/1")
