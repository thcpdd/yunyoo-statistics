import http.cookiejar
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock

from yunyoo_clash import JWT_COOKIE, LoginError, YunyooClient


HOST = {"bwusage": 1, "bwlimit": 100, "nextduedate": 1893456000}


def add_cookie(client, name=JWT_COOKIE, expires=None):
    client.cookies.set_cookie(http.cookiejar.Cookie(
        version=0, name=name, value="test-value", port=None, port_specified=False,
        domain="yunyoo.cc", domain_specified=False, domain_initial_dot=False,
        path="/", path_specified=True, secure=True,
        expires=expires if expires is not None else int(time.time()) + 3600,
        discard=False, comment=None, comment_url=None, rest={}, rfc2109=False,
    ))


class CacheTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "auth.cookies"

    def cached_client(self):
        client = YunyooClient(cache_file=self.path)
        add_cookie(client)
        add_cookie(client, "PHPSESSID")
        client.save_cache()
        return YunyooClient(cache_file=self.path)

    def test_roundtrip_and_private_permissions(self):
        client = self.cached_client()
        self.assertTrue(client.load_cache())
        self.assertEqual(client.jwt, "test-value")
        self.assertEqual({c.name for c in client.cookies}, {JWT_COOKIE, "PHPSESSID"})
        if os.name == "posix":
            self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)

    def test_cache_hit_does_not_read_password_or_login(self):
        client = self.cached_client()
        client.subscription_info = Mock(return_value=HOST)
        client.login = Mock()
        password = Mock(side_effect=AssertionError("Password should not be read"))
        self.assertIn("upload=536870912", client.userinfo("test@example.com", password, 1))
        client.login.assert_not_called()
        password.assert_not_called()

    def test_failed_cached_request_logs_in_once_and_replaces_cache(self):
        client = self.cached_client()
        client.subscription_info = Mock(side_effect=[LoginError("expired"), HOST])
        def login(email, password):
            self.assertFalse(self.path.exists())
            self.assertEqual(list(client.cookies), [])
            add_cookie(client)
        client.login = Mock(side_effect=login)
        password = Mock(return_value="test-password")
        client.userinfo("test@example.com", password, 1)
        client.login.assert_called_once_with("test@example.com", "test-password")
        self.assertEqual(client.subscription_info.call_count, 2)
        self.assertTrue(self.path.exists())

    def test_retry_failure_does_not_loop(self):
        client = self.cached_client()
        client.subscription_info = Mock(side_effect=LoginError("network failure"))
        client.login = Mock(side_effect=lambda *_: add_cookie(client))
        with self.assertRaises(LoginError):
            client.userinfo("test@example.com", lambda: "test-password", 1)
        self.assertEqual(client.subscription_info.call_count, 2)
        client.login.assert_called_once()

    def test_invalid_and_expired_cache_are_ignored(self):
        client = YunyooClient(cache_file=self.path)
        self.path.write_text("not a cookie jar")
        self.assertFalse(client.load_cache())
        add_cookie(client, expires=int(time.time()) - 60)
        client.save_cache()
        self.assertFalse(client.load_cache())
        self.assertIsNone(client.jwt)

    def test_missing_cache_logs_in_and_saves(self):
        client = YunyooClient(cache_file=self.path)
        client.login = Mock(side_effect=lambda *_: add_cookie(client))
        client.subscription_info = Mock(return_value=HOST)
        client.userinfo("test@example.com", lambda: "test-password", 1)
        client.login.assert_called_once()
        self.assertTrue(self.path.exists())


if __name__ == "__main__":
    unittest.main()
