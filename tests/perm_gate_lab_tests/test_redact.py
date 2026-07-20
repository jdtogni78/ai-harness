"""Redactor tests.

Fixtures use obviously synthetic values that pattern-match real credentials
but are not real secrets. If you're tempted to paste a real key here as a
'better test', don't — that's the exact failure mode this module exists to
prevent.
"""

from __future__ import annotations

import unittest

from perm_gate_lab.redact import redact, redact_many


class TestRedact(unittest.TestCase):
    def test_none_and_empty_pass_through(self) -> None:
        self.assertEqual(redact(None), (None, False))
        self.assertEqual(redact(""), ("", False))

    def test_plain_text_unchanged(self) -> None:
        out, hit = redact("ls -la /tmp")
        self.assertFalse(hit)
        self.assertEqual(out, "ls -la /tmp")

    def test_anthropic_key(self) -> None:
        s = "export ANTHROPIC_API_KEY=sk-ant-abcdefghij1234567890XYZ"
        out, hit = redact(s)
        self.assertTrue(hit)
        self.assertNotIn("sk-ant-abcdefghij1234567890XYZ", out)
        # The ENV_SECRET pattern fires first on the KEY=VALUE shape.
        self.assertIn("[REDACTED:", out)

    def test_openai_key(self) -> None:
        out, hit = redact("curl -H 'Authorization: sk-proj1234567890ABCDEFGH'")
        self.assertTrue(hit)
        self.assertNotIn("sk-proj1234567890ABCDEFGH", out)

    def test_aws_access_key(self) -> None:
        out, hit = redact("AWS_ACCESS_KEY_ID=AKIAIOSFODNN7EXAMPLE")
        self.assertTrue(hit)
        self.assertNotIn("AKIAIOSFODNN7EXAMPLE", out)

    def test_github_token(self) -> None:
        out, hit = redact("token=ghp_abcdefghijklmnopqrstuvwxyz0123456789")
        self.assertTrue(hit)
        self.assertNotIn("ghp_abcdefghijklmnopqrstuvwxyz0123456789", out)

    def test_bearer_token(self) -> None:
        out, hit = redact("Authorization: Bearer abcdefghij1234567890ZYXWV")
        self.assertTrue(hit)
        self.assertNotIn("abcdefghij1234567890ZYXWV", out)

    def test_ssh_path(self) -> None:
        out, hit = redact("cat /Users/alice/.ssh/id_rsa")
        self.assertTrue(hit)
        self.assertNotIn("/Users/alice/.ssh/id_rsa", out)

    def test_env_secret_preserves_key_name(self) -> None:
        out, hit = redact("DB_PASSWORD=hunter2longenoughval")
        self.assertTrue(hit)
        self.assertIn("DB_PASSWORD=", out)
        self.assertNotIn("hunter2longenoughval", out)

    def test_pem_block(self) -> None:
        pem = (
            "-----BEGIN RSA PRIVATE KEY-----\n"
            "MIIEpAIBAAKCAQEAabc...redacted-on-line...\n"
            "-----END RSA PRIVATE KEY-----"
        )
        out, hit = redact(f"key: {pem}")
        self.assertTrue(hit)
        self.assertNotIn("MIIEpAIBAAKCAQEA", out)

    def test_multiple_patterns_one_pass(self) -> None:
        s = "AKIAIOSFODNN7EXAMPLE and Bearer abcdefghij1234567890ZYXWV"
        out, hit = redact(s)
        self.assertTrue(hit)
        self.assertNotIn("AKIAIOSFODNN7EXAMPLE", out)
        self.assertNotIn("abcdefghij1234567890ZYXWV", out)

    def test_redact_many(self) -> None:
        outs, any_hit = redact_many(["safe", "AKIAIOSFODNN7EXAMPLE", None, ""])
        self.assertTrue(any_hit)
        self.assertEqual(outs[0], "safe")
        self.assertNotIn("AKIAIOSFODNN7EXAMPLE", outs[1] or "")
        self.assertIsNone(outs[2])
        self.assertEqual(outs[3], "")


if __name__ == "__main__":
    unittest.main()
