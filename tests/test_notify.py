"""Tests for bot/notify.py: URL parsing, redaction, per-service request
shape, length caps, and never-raise sending. No network — urlopen is patched
throughout."""

import json
import unittest
from unittest import mock

import support

notify = support.load_notify()


class ParseNtfyTest(unittest.TestCase):
    def test_ntfy_scheme_is_plain_http(self):
        t = notify._parse_one("ntfy://192.168.1.10/aniloads")
        self.assertEqual(t["kind"], "ntfy")
        self.assertEqual(t["url"], "http://192.168.1.10/aniloads")

    def test_ntfys_scheme_is_https(self):
        t = notify._parse_one("ntfys://ntfy.example.com/aniloads")
        self.assertEqual(t["url"], "https://ntfy.example.com/aniloads")

    def test_ntfy_with_port_and_credentials(self):
        t = notify._parse_one("ntfy://user:pass@host:8080/topic")
        self.assertEqual(t["url"], "http://host:8080/topic")
        self.assertEqual(t["auth"], ("user", "pass"))

    def test_plain_ntfy_sh_recognised(self):
        t = notify._parse_one("https://ntfy.sh/my-topic")
        self.assertEqual(t["kind"], "ntfy")
        self.assertEqual(t["url"], "https://ntfy.sh/my-topic")

    def test_plain_self_hosted_https_not_recognised(self):
        # Only the public ntfy.sh host is unambiguous as plain https:// — a
        # self-hosted server must use the explicit ntfy://ntfys:// scheme.
        self.assertIsNone(notify._parse_one("https://my-ntfy.example.com/topic"))

    def test_missing_topic_is_rejected(self):
        self.assertIsNone(notify._parse_one("ntfy://host/"))

    def test_missing_host_is_rejected(self):
        self.assertIsNone(notify._parse_one("ntfy:///topic"))

    def test_credentials_are_percent_decoded(self):
        # urlsplit leaves userinfo percent-encoded; a password containing
        # '@', ':', '/' or '%' must be %-encoded in the URL and decoded back
        # before use, or it authenticates with the wrong string.
        t = notify._parse_one("ntfy://me:p%40ss%3Aword@host/topic")
        self.assertEqual(t["auth"], ("me", "p@ss:word"))

    def test_credentials_without_special_chars_round_trip(self):
        t = notify._parse_one("ntfy://user:pass@host/topic")
        self.assertEqual(t["auth"], ("user", "pass"))


class ParseDiscordTest(unittest.TestCase):
    def test_discord_scheme(self):
        t = notify._parse_one("discord://123456/abcDEF")
        self.assertEqual(t["kind"], "discord")
        self.assertEqual(t["url"], "https://discord.com/api/webhooks/123456/abcDEF")

    def test_plain_discord_com_webhook_url(self):
        t = notify._parse_one("https://discord.com/api/webhooks/123456/abcDEF")
        self.assertEqual(t["kind"], "discord")
        self.assertEqual(t["url"], "https://discord.com/api/webhooks/123456/abcDEF")

    def test_plain_discordapp_com_webhook_url(self):
        t = notify._parse_one("https://discordapp.com/api/webhooks/123456/abcDEF")
        self.assertEqual(t["kind"], "discord")

    def test_discord_scheme_missing_token_rejected(self):
        self.assertIsNone(notify._parse_one("discord://123456/"))

    def test_plain_url_wrong_shape_rejected(self):
        self.assertIsNone(notify._parse_one("https://discord.com/api/other/123456/abcDEF"))


class ParseGotifyTest(unittest.TestCase):
    def test_gotify_scheme_is_plain_http(self):
        t = notify._parse_one("gotify://192.168.1.10/tok123")
        self.assertEqual(t["kind"], "gotify")
        self.assertEqual(t["url"], "http://192.168.1.10/message")
        self.assertEqual(t["token"], "tok123")

    def test_gotifys_scheme_is_https(self):
        t = notify._parse_one("gotifys://gotify.example.com/tok123")
        self.assertEqual(t["url"], "https://gotify.example.com/message")

    def test_gotify_with_port(self):
        t = notify._parse_one("gotify://host:8888/tok123")
        self.assertEqual(t["url"], "http://host:8888/message")

    def test_missing_token_rejected(self):
        self.assertIsNone(notify._parse_one("gotify://host/"))


class ParseTargetsTest(unittest.TestCase):
    def test_empty_or_none_returns_no_targets(self):
        self.assertEqual(notify.parse_targets(""), [])
        self.assertEqual(notify.parse_targets(None), [])

    def test_comma_separated_list_parses_every_entry(self):
        targets = notify.parse_targets(
            "ntfys://ntfy.sh/topic, discord://123/tok , gotify://host/tok"
        )
        self.assertEqual([t["kind"] for t in targets], ["ntfy", "discord", "gotify"])

    def test_unknown_scheme_is_skipped_with_one_warning(self):
        with self.assertLogs("notify", level="WARNING") as cm:
            targets = notify.parse_targets("foo://bar/baz")
        self.assertEqual(targets, [])
        self.assertEqual(len(cm.output), 1)

    def test_unknown_entry_does_not_disable_the_rest(self):
        with self.assertLogs("notify", level="WARNING"):
            targets = notify.parse_targets("foo://bar/baz,ntfys://ntfy.sh/topic")
        self.assertEqual(len(targets), 1)
        self.assertEqual(targets[0]["kind"], "ntfy")

    def test_blank_entries_are_ignored(self):
        targets = notify.parse_targets("ntfys://ntfy.sh/topic,,  ,")
        self.assertEqual(len(targets), 1)


class RedactionTest(unittest.TestCase):
    def test_redacted_url_never_contains_topic_or_token(self):
        secret_url = "ntfys://user:hunter2@ntfy.example.com/super-secret-topic"
        redacted = notify._redact(secret_url)
        self.assertNotIn("super-secret-topic", redacted)
        self.assertNotIn("hunter2", redacted)
        self.assertIn("ntfy.example.com", redacted)

    def test_unknown_scheme_warning_redacts_the_url(self):
        with self.assertLogs("notify", level="WARNING") as cm:
            notify.parse_targets("badscheme://host/some-secret-token")
        joined = "\n".join(cm.output)
        self.assertNotIn("some-secret-token", joined)

    def test_send_failure_warning_redacts_the_url(self):
        target = {"kind": "ntfy", "url": "https://ntfy.example.com/secret-topic-xyz"}
        with mock.patch.object(notify, "_send_one", side_effect=OSError("boom")):
            with self.assertLogs("notify", level="WARNING") as cm:
                notify.send_all([target], "Aniloads", "hello")
        joined = "\n".join(cm.output)
        self.assertNotIn("secret-topic-xyz", joined)


class RequestShapeTest(unittest.TestCase):
    """Verify the request method/URL/headers/body built per service, with
    urlopen patched so nothing touches the network."""

    def _sent_request(self, target, title, message):
        with mock.patch("notify.urllib.request.urlopen") as mock_urlopen:
            notify._send_one(target, title, message)
        self.assertEqual(mock_urlopen.call_count, 1)
        (req,), kwargs = mock_urlopen.call_args
        return req, kwargs

    def test_ntfy_request(self):
        target = {"kind": "ntfy", "url": "https://ntfy.example.com/topic"}
        req, kwargs = self._sent_request(target, "Aniloads", "hello world")
        self.assertEqual(req.full_url, "https://ntfy.example.com/topic")
        self.assertEqual(req.get_method(), "POST")
        self.assertEqual(req.get_header("Title"), "Aniloads")
        self.assertEqual(req.data, b"hello world")
        self.assertEqual(kwargs.get("timeout"), notify.TIMEOUT_SECONDS)

    def test_ntfy_request_with_auth_sends_basic_header(self):
        target = {"kind": "ntfy", "url": "https://ntfy.example.com/topic", "auth": ("u", "p")}
        req, _ = self._sent_request(target, "Aniloads", "hello")
        self.assertTrue(req.get_header("Authorization").startswith("Basic "))

    def test_discord_request(self):
        target = {"kind": "discord", "url": "https://discord.com/api/webhooks/1/tok"}
        req, _ = self._sent_request(target, "Aniloads", "hello world")
        self.assertEqual(req.full_url, "https://discord.com/api/webhooks/1/tok")
        self.assertEqual(req.get_method(), "POST")
        self.assertEqual(req.get_header("Content-type"), "application/json")
        body = json.loads(req.data.decode("utf-8"))
        self.assertEqual(body, {"content": "hello world"})

    def test_gotify_request(self):
        target = {"kind": "gotify", "url": "https://gotify.example.com/message", "token": "tok123"}
        req, _ = self._sent_request(target, "Aniloads", "hello world")
        self.assertEqual(req.full_url, "https://gotify.example.com/message?token=tok123")
        self.assertEqual(req.get_method(), "POST")
        body = json.loads(req.data.decode("utf-8"))
        self.assertEqual(body["title"], "Aniloads")
        self.assertEqual(body["message"], "hello world")
        self.assertIn("priority", body)

    def test_ntfy_request_sends_explicit_user_agent(self):
        target = {"kind": "ntfy", "url": "https://ntfy.example.com/topic"}
        req, _ = self._sent_request(target, "Aniloads", "hello")
        self.assertEqual(req.get_header("User-agent"), notify.USER_AGENT)

    def test_discord_request_sends_explicit_user_agent(self):
        # Discord sits behind Cloudflare, which 403s urllib's default
        # "Python-urllib/x.y" User-Agent — this is the regression this test
        # guards against.
        target = {"kind": "discord", "url": "https://discord.com/api/webhooks/1/tok"}
        req, _ = self._sent_request(target, "Aniloads", "hello")
        self.assertEqual(req.get_header("User-agent"), notify.USER_AGENT)
        self.assertNotIn("python-urllib", req.get_header("User-agent").lower())

    def test_gotify_request_sends_explicit_user_agent(self):
        target = {"kind": "gotify", "url": "https://gotify.example.com/message", "token": "tok123"}
        req, _ = self._sent_request(target, "Aniloads", "hello")
        self.assertEqual(req.get_header("User-agent"), notify.USER_AGENT)


class TruncationTest(unittest.TestCase):
    def test_discord_message_truncated_to_2000_chars(self):
        target = {"kind": "discord", "url": "https://discord.com/api/webhooks/1/tok"}
        long_message = "x" * 3000
        with mock.patch("notify.urllib.request.urlopen") as mock_urlopen:
            notify._send_one(target, "Aniloads", long_message)
        (req,), _ = mock_urlopen.call_args
        body = json.loads(req.data.decode("utf-8"))
        self.assertLessEqual(len(body["content"]), notify.DISCORD_LIMIT)

    def test_ntfy_message_truncated_below_generic_limit(self):
        target = {"kind": "ntfy", "url": "https://ntfy.example.com/topic"}
        long_message = "x" * 10000
        with mock.patch("notify.urllib.request.urlopen") as mock_urlopen:
            notify._send_one(target, "Aniloads", long_message)
        (req,), _ = mock_urlopen.call_args
        self.assertLessEqual(len(req.data), notify.GENERIC_LIMIT)

    def test_short_message_is_untouched(self):
        self.assertEqual(notify._truncate("short", 100), "short")


class SendAllNeverRaisesTest(unittest.TestCase):
    def test_urlopen_exception_is_swallowed(self):
        target = {"kind": "ntfy", "url": "https://ntfy.example.com/topic"}
        with mock.patch("notify.urllib.request.urlopen", side_effect=OSError("timed out")):
            notify.send_all([target], "Aniloads", "hello")  # must not raise

    def test_one_bad_target_does_not_stop_the_rest(self):
        bad = {"kind": "ntfy", "url": "https://bad.example.com/topic"}
        good = {"kind": "discord", "url": "https://discord.com/api/webhooks/1/tok"}
        calls = []

        def fake_send_one(target, title, message):
            calls.append(target["kind"])
            if target is bad:
                raise OSError("unreachable")

        with mock.patch.object(notify, "_send_one", side_effect=fake_send_one):
            notify.send_all([bad, good], "Aniloads", "hello")
        self.assertEqual(calls, ["ntfy", "discord"])

    def test_empty_target_list_is_a_noop(self):
        notify.send_all([], "Aniloads", "hello")


if __name__ == "__main__":
    unittest.main()
