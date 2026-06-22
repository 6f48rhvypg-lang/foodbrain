import unittest
from unittest import mock
from urllib.error import URLError

from foodbrain_assistant import llm
from foodbrain_assistant.llm import LlmError, http_post


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


if __name__ == "__main__":
    unittest.main()
