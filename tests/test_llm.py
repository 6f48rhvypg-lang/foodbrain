import json
import unittest
from unittest import mock
from urllib.error import URLError

from foodbrain_assistant import llm
from foodbrain_assistant.config import Settings
from foodbrain_assistant.llm import LlmError, http_post, post_chat_json


class _FakeResponse:
    def __init__(self, body: bytes) -> None:
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return self._body


class HttpPostRetryTest(unittest.TestCase):
    def test_transient_url_error_is_retried_once(self) -> None:
        attempts = []

        def flaky_urlopen(request, timeout=None):
            attempts.append(1)
            if len(attempts) == 1:
                raise URLError("connection reset")
            return _FakeResponse(b'{"ok": true}')

        with mock.patch.object(llm, "urlopen", flaky_urlopen):
            raw = http_post("http://x", {}, b"{}", 5)
        self.assertEqual(raw, '{"ok": true}')
        self.assertEqual(len(attempts), 2)  # failed once, retried, succeeded

    def test_gives_up_after_one_retry(self) -> None:
        attempts = []

        def always_fails(request, timeout=None):
            attempts.append(1)
            raise URLError("down")

        with mock.patch.object(llm, "urlopen", always_fails):
            with self.assertRaises(LlmError):
                http_post("http://x", {}, b"{}", 5)
        self.assertEqual(len(attempts), 2)  # original + one retry


def _settings(**overrides) -> Settings:
    base = dict(grocy_base_url=None, grocy_api_key=None, openrouter_api_key="test-key")
    base.update(overrides)
    return Settings(**base)


class PostChatJsonHistoryTest(unittest.TestCase):
    def _capture(self):
        captured = {}

        def transport(url, headers, body, timeout):
            captured["body"] = json.loads(body.decode("utf-8"))
            return json.dumps({"choices": [{"message": {"content": "{\"ok\": true}"}}]})

        return transport, captured

    def test_history_inserted_between_system_and_final_user(self) -> None:
        transport, captured = self._capture()
        post_chat_json(
            settings=_settings(),
            model="m",
            system="SYS",
            user="latest question",
            history=[
                {"role": "user", "content": "erste Frage"},
                {"role": "assistant", "content": "erste Antwort"},
            ],
            transport=transport,
        )
        roles = [m["role"] for m in captured["body"]["messages"]]
        self.assertEqual(roles, ["system", "user", "assistant", "user"])
        self.assertEqual(captured["body"]["messages"][0]["content"], "SYS")
        self.assertEqual(captured["body"]["messages"][-1]["content"], "latest question")

    def test_malformed_history_entries_are_dropped(self) -> None:
        transport, captured = self._capture()
        post_chat_json(
            settings=_settings(),
            model="m",
            system="SYS",
            user="q",
            history=[
                {"role": "system", "content": "nope"},  # wrong role
                {"role": "user", "content": "   "},       # blank
                "garbage",                                  # not a dict
                {"role": "assistant", "content": "keep me"},
            ],
            transport=transport,
        )
        msgs = captured["body"]["messages"]
        self.assertEqual([m["role"] for m in msgs], ["system", "assistant", "user"])
        self.assertEqual(msgs[1]["content"], "keep me")

    def test_no_history_keeps_system_user_only(self) -> None:
        transport, captured = self._capture()
        post_chat_json(settings=_settings(), model="m", system="SYS", user="q", transport=transport)
        self.assertEqual([m["role"] for m in captured["body"]["messages"]], ["system", "user"])


if __name__ == "__main__":
    unittest.main()
