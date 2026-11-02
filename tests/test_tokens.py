import os
from unittest import mock

from cronward import tokens
from tests.harness import BIN, CronwardTest


class TokensTest(CronwardTest):
    def test_encrypted_with_systemd_creds(self):
        how = tokens.store("personal", "github_pat_abc")
        self.assertIn("encrypted", how)
        self.assertEqual(tokens.kind("personal"), "encrypted")
        cred = tokens.token_dir() / "personal.cred"
        self.assertNotIn("github_pat_abc", cred.read_text())
        self.assertEqual(tokens.load("personal"), "github_pat_abc")
        self.assertIn("--user", self.calls("systemd-creds")[0])
        self.assertEqual(oct(tokens.token_dir().stat().st_mode & 0o777), "0o700")

    def test_a_credential_for_another_account_does_not_decrypt(self):
        tokens.store("personal", "github_pat_abc")
        os.rename(tokens.token_dir() / "personal.cred", tokens.token_dir() / "work.cred")
        with self.assertRaisesRegex(tokens.TokenError, "cannot decrypt"):
            tokens.load("work")

    def test_plain_file_without_systemd_creds(self):
        path = os.pathsep.join(p for p in os.environ["PATH"].split(os.pathsep)
                               if p != str(BIN) and not os.path.exists(f"{p}/systemd-creds"))
        with mock.patch.dict(os.environ, {"PATH": path}):
            how = tokens.store("personal", "github_pat_abc")
        self.assertIn("not installed", how)
        plain = tokens.token_dir() / "personal.token"
        self.assertEqual(oct(plain.stat().st_mode & 0o777), "0o600")
        self.assertEqual(tokens.load("personal"), "github_pat_abc")
        os.chmod(plain, 0o644)
        with self.assertRaisesRegex(tokens.TokenError, "chmod 600"):
            tokens.load("personal")

    def test_plain_file_when_encryption_fails(self):
        with mock.patch.dict(os.environ, {"CW_CREDS": "fail"}):
            how = tokens.store("personal", "github_pat_abc")
        self.assertIn("could not encrypt", how)
        self.assertEqual(tokens.kind("personal"), "plain")

    def test_storing_again_replaces_the_other_kind(self):
        with mock.patch.dict(os.environ, {"CW_CREDS": "fail"}):
            tokens.store("personal", "old")
        tokens.store("personal", "new")
        self.assertEqual(sorted(p.name for p in tokens.token_dir().iterdir()),
                         ["personal.cred"])
        self.assertEqual(tokens.load("personal"), "new")

    def test_missing_and_remove(self):
        with self.assertRaisesRegex(tokens.TokenError, "cronward token set personal"):
            tokens.load("personal")
        tokens.store("personal", "x")
        self.assertTrue(tokens.remove("personal"))
        self.assertFalse(tokens.remove("personal"))
