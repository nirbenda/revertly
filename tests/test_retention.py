"""Tests for revertly.retention — the shared prune planner + safety invariants.

Run:  python3 -m unittest tests.test_retention -v
"""
import time
import unittest

from revertly.retention import SessionInfo, plan


def S(id, age_days, size, flagged=False, ended=True, is_revert=False):
    now = time.time()
    return SessionInfo(
        id=id, started=now - age_days * 86400,
        ended=(now - age_days * 86400 + 60) if ended else None,
        size=size, flagged=flagged, is_revert=is_revert)


class TestPlan(unittest.TestCase):
    def ids(self, items):
        return {i.id for i in items}

    def test_keep_days_prunes_old(self):
        sess = [S("new", 1, 100), S("old", 40, 100)]
        self.assertEqual(self.ids(plan(sess, keep_days=30)), {"old"})

    def test_live_session_never_pruned(self):
        # unsealed (ended=None) is live — even with clear_all it survives
        sess = [S("done", 100, 100), S("live", 100, 100, ended=False)]
        self.assertEqual(self.ids(plan(sess, clear_all=True)), {"done"})

    def test_flagged_protected_unless_included(self):
        sess = [S("plain", 100, 100), S("evi", 100, 100, flagged=True)]
        self.assertEqual(self.ids(plan(sess, clear_all=True)), {"plain"})
        self.assertEqual(self.ids(plan(sess, clear_all=True, include_flagged=True)),
                         {"plain", "evi"})

    def test_before_cutoff(self):
        now = time.time()
        sess = [S("a", 1, 100), S("b", 5, 100), S("c", 10, 100)]
        cutoff = now - 3 * 86400   # older than 3 days
        self.assertEqual(self.ids(plan(sess, before=cutoff)), {"b", "c"})

    def test_disk_cap_prunes_oldest_first(self):
        # total 300, cap 150 -> must drop oldest until <=150: drop c(100) then
        # b(100) -> remaining 100 <= 150. a (newest) kept.
        sess = [S("a", 1, 100), S("b", 5, 100), S("c", 10, 100)]
        got = self.ids(plan(sess, max_disk_bytes=150))
        self.assertEqual(got, {"b", "c"})

    def test_disk_cap_skips_flagged(self):
        # oldest is flagged -> not counted for pruning; next oldest goes
        sess = [S("a", 1, 100), S("old_flag", 10, 100, flagged=True),
                S("b", 5, 100)]
        got = self.ids(plan(sess, max_disk_bytes=150))
        self.assertNotIn("old_flag", got)

    def test_nothing_when_all_recent(self):
        sess = [S("a", 1, 100), S("b", 2, 100)]
        self.assertEqual(plan(sess, keep_days=30), [])

    def test_reasons_recorded(self):
        sess = [S("old", 40, 100)]
        items = plan(sess, keep_days=30)
        self.assertEqual(len(items), 1)
        self.assertIn("older than", items[0].reason)


if __name__ == "__main__":
    unittest.main()
