import threading
import unittest

from core.run_progress import RunLock


class RunProgressTests(unittest.TestCase):
    def setUp(self):
        self.now = 0.0
        self.lock = RunLock(lambda: self.now)

    def test_new_short_lock_never_inherits_previous_age_between_samples(self):
        self.lock.acquire()
        self.now = 1000
        old = self.lock.stalled(900)
        self.assertIsNotNone(old)
        self.lock.release()
        self.lock.acquire()
        self.now += 20
        self.assertIsNone(self.lock.stalled(900))
        self.assertNotEqual(old.generation, self.lock.snapshot().generation)
        self.lock.release()
        self.assertIsNone(self.lock.snapshot())

    def test_real_progress_extends_deadline_without_resetting_task_start(self):
        self.lock.acquire()
        for _ in range(5):
            self.now += 800
            self.lock.progress()
            self.assertIsNone(self.lock.stalled(900))
        self.assertEqual(self.lock.snapshot().started_at, 0)
        self.now += 901
        self.assertIsNotNone(self.lock.stalled(900))
        self.lock.release()

    def test_progress_during_diagnostics_cancels_exit(self):
        self.lock.acquire()
        self.now = 901
        snapshot = self.lock.stalled(900)
        self.lock.progress()
        calls = []
        self.assertFalse(self.lock.if_still_stalled(snapshot, 900, lambda: calls.append(78)))
        self.assertEqual(calls, [])
        self.lock.release()

    def test_finished_task_cancels_exit_even_if_next_task_is_stuck(self):
        self.lock.acquire()
        self.now = 901
        old = self.lock.stalled(900)
        self.lock.release()
        self.lock.acquire()
        self.now = 2000
        calls = []
        self.assertFalse(self.lock.if_still_stalled(old, 900, lambda: calls.append(78)))
        fresh = self.lock.stalled(900)
        self.assertTrue(self.lock.if_still_stalled(fresh, 900, lambda: calls.append(78)))
        self.assertEqual(calls, [78])
        self.lock.release()

    def test_worker_can_release_the_parent_acquired_lock(self):
        self.assertTrue(self.lock.acquire(blocking=False))
        self.assertFalse(self.lock.acquire(blocking=False))
        thread = threading.Thread(target=self.lock.release)
        thread.start(); thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertFalse(self.lock.locked())
        self.assertIsNone(self.lock.snapshot())


if __name__ == '__main__':
    unittest.main()
