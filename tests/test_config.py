"""Tests for revertly.config parsing — especially the forgiving retention keys
so the documented config.toml actually takes effect.

Run:  python3 -m unittest tests.test_config -v
"""
import os
import tempfile
import unittest

from revertly.config import load, parse_days, parse_gb


class TestValueParsers(unittest.TestCase):
    def test_parse_days(self):
        self.assertEqual(parse_days(30), 30)
        self.assertEqual(parse_days("30"), 30)
        self.assertEqual(parse_days("30d"), 30)
        self.assertEqual(parse_days("4w"), 28)
        self.assertIsNone(parse_days("nonsense"))

    def test_parse_gb(self):
        self.assertEqual(parse_gb(10), 10.0)
        self.assertEqual(parse_gb("10"), 10.0)
        self.assertEqual(parse_gb("10GB"), 10.0)
        self.assertEqual(parse_gb("1TB"), 1024.0)
        self.assertAlmostEqual(parse_gb("512MB"), 0.5)
        self.assertIsNone(parse_gb("huge"))


class TestRetentionKeys(unittest.TestCase):
    def _load(self, body):
        with tempfile.NamedTemporaryFile("w", suffix=".toml", delete=False) as f:
            f.write(body)
            path = f.name
        try:
            return load(path)
        finally:
            os.remove(path)

    def test_documented_human_form_works(self):
        # the exact PRODUCT.md-style keys/values must take effect, not be dropped
        cfg = self._load('[retention]\nsessions = "30d"\nmax_disk = "10GB"\n')
        self.assertEqual(cfg.retention_days, 30)
        self.assertEqual(cfg.max_disk_gb, 10.0)

    def test_canonical_form_works(self):
        cfg = self._load('[retention]\nsessions_days = 7\nmax_disk_gb = 5\n')
        self.assertEqual(cfg.retention_days, 7)
        self.assertEqual(cfg.max_disk_gb, 5.0)

    def test_fallback_retention_default_and_alias(self):
        # default is a shorter window for non-CoW (full-copy) filesystems
        self.assertEqual(self._load('').fallback_retention_days, 7)
        cfg = self._load('[retention]\nfallback_days = "2w"\n')
        self.assertEqual(cfg.fallback_retention_days, 14)

    def test_natural_field_name_alias(self):
        cfg = self._load('[retention]\nretention_days = 14\n')
        self.assertEqual(cfg.retention_days, 14)


if __name__ == "__main__":
    unittest.main()
