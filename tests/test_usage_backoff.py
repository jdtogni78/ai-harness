import unittest

from remote_control.usage_limit.detect import compute_backoff

BACKOFFS = (300, 900, 1800)


class BackoffTest(unittest.TestCase):
    def test_first_attempt_uses_first_step(self):
        nxt, msg = compute_backoff("monthly usage limit", attempts=1, now=0,
                                   backoffs_secs=BACKOFFS)
        self.assertEqual(nxt, 300)
        self.assertIn("300s", msg)

    def test_second_and_third_steps(self):
        self.assertEqual(compute_backoff("usage limit", 2, 0, BACKOFFS)[0], 900)
        self.assertEqual(compute_backoff("usage limit", 3, 0, BACKOFFS)[0], 1800)

    def test_caps_at_last_step(self):
        self.assertEqual(compute_backoff("usage limit", 9, 0, BACKOFFS)[0], 1800)

    def test_zero_attempts_floors_to_first_step(self):
        self.assertEqual(compute_backoff("usage limit", 0, 0, BACKOFFS)[0], 300)

    def test_now_is_added(self):
        self.assertEqual(compute_backoff("usage limit", 1, 5000, BACKOFFS)[0], 5300)

    def test_reset_time_overrides_backoff(self):
        nxt, msg = compute_backoff("resets 7:50pm UTC", attempts=5, now=0,
                                   backoffs_secs=BACKOFFS)
        self.assertIn("reset", msg)
        self.assertNotIn("next attempt in", msg)
        self.assertGreater(nxt, 0)


if __name__ == "__main__":
    unittest.main()
