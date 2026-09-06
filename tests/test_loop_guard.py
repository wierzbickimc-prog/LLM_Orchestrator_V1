from __future__ import annotations

import unittest

from router import loop_guard as lg
from router.loop_guard import RepetitionDetector


class RepetitionDetectorTests(unittest.TestCase):
    def test_no_alert_on_normal_prose(self) -> None:
        detector = RepetitionDetector()
        text = (
            "The quick brown fox jumps over the lazy dog and then trots off into "
            "the sunset, thinking about breakfast and other important matters. "
        )
        for char in text:
            self.assertIsNone(detector.feed(char))

    def test_no_alert_on_bulleted_list(self) -> None:
        detector = RepetitionDetector()
        markdown = "\n".join(f"- item number {i} with some detail" for i in range(20))
        for char in markdown:
            self.assertIsNone(detector.feed(char))

    def test_flags_arbitrary_length_repeat(self) -> None:
        detector = RepetitionDetector()
        phrase = "I am stuck in a loop again. "
        alert = None
        for char in phrase * 8:
            result = detector.feed(char)
            if result is not None:
                alert = result
        self.assertIsNotNone(alert)
        assert alert is not None
        self.assertEqual(alert.snippet, phrase)
        self.assertGreaterEqual(alert.repeats, 3)


class StreamGuardTests(unittest.TestCase):
    def test_alert_lifecycle_ack_and_stop(self) -> None:
        guard = lg.start_stream("scout")
        self.assertEqual(lg.status(), {"active": False, "should_alert": False})

        lg.feed(guard, "loop token " * 6)
        status = lg.status()
        self.assertTrue(status["should_alert"])
        self.assertEqual(status["phase"], "scout")

        self.assertTrue(lg.ack(status["id"], status["seq"]))
        self.assertFalse(lg.status()["should_alert"])

        self.assertTrue(lg.request_stop(status["id"]))
        self.assertTrue(guard.stop_requested)

    def test_stop_and_ack_reject_stale_ids(self) -> None:
        lg.start_stream("scout")
        self.assertFalse(lg.request_stop(-1))
        self.assertFalse(lg.ack(-1, 0))


if __name__ == "__main__":
    unittest.main()
