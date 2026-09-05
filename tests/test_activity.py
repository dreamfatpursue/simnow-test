import tempfile
import unittest
from pathlib import Path

from live_grid.activity import ActivityIdentity, ActivityLock


def identity(audit_dir: str = "audit/run") -> ActivityIdentity:
    return ActivityIdentity(
        run_id="run-1",
        pid=123,
        started_at="2026-09-04T01:02:03+00:00",
        environment="first",
        market_data_mode="normal",
        strategy_hash="hash",
        contracts=("rb2601@SHFE", "AP610@CZCE"),
        audit_dir=audit_dir,
    )


class ActivityLockTests(unittest.TestCase):
    def test_lock_exposes_identity_only_while_held_and_releases_for_next_run(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "activity.lock"
            held, current = ActivityLock.try_acquire(path, identity())
            self.assertIsNotNone(held)
            self.assertIsNone(current)
            self.assertEqual(ActivityLock.read_active(path), identity())

            blocked, current = ActivityLock.try_acquire(path, identity("audit/other"))
            self.assertIsNone(blocked)
            self.assertEqual(current, identity())

            held.release()
            self.assertIsNone(ActivityLock.read_active(path))
            next_lock, current = ActivityLock.try_acquire(path, identity("audit/other"))
            self.assertIsNotNone(next_lock)
            self.assertIsNone(current)
            next_lock.release()

    def test_identity_update_is_visible_without_exposing_credentials(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "activity.lock"
            held, _ = ActivityLock.try_acquire(path, identity())
            updated = identity("audit/updated")
            held.update_identity(updated)
            self.assertEqual(ActivityLock.read_active(path), updated)
            content = path.read_text(encoding="utf-8")
            self.assertNotIn("password", content)
            self.assertNotIn("account", content)
            held.release()


if __name__ == "__main__":
    unittest.main()
