"""Tests for revertly.notify — cross-platform best-effort desktop notification.

Run:  python3 -m unittest tests.test_notify -v
"""
import os
import unittest
from unittest import mock

from revertly import notify


class TestDesktopNotify(unittest.TestCase):
    def test_no_notify_env_suppresses(self):
        with mock.patch.dict(os.environ, {"REVERTLY_NO_NOTIFY": "1"}), \
             mock.patch("revertly.notify.subprocess.Popen") as popen:
            notify.desktop("Title", "Body")
            popen.assert_not_called()

    def test_linux_uses_notify_send(self):
        with mock.patch.dict(os.environ, {}, clear=False), \
             mock.patch("revertly.notify.sys.platform", "linux"), \
             mock.patch("revertly.notify.shutil.which",
                        side_effect=lambda n: "/usr/bin/notify-send" if n == "notify-send" else None), \
             mock.patch("revertly.notify.subprocess.Popen") as popen:
            os.environ.pop("REVERTLY_NO_NOTIFY", None)
            notify.desktop("Title", "Body")
            self.assertTrue(popen.called)
            argv = popen.call_args[0][0]
            self.assertEqual(argv[0], "notify-send")
            self.assertIn("revertly: Title", argv)
            self.assertIn("Body", argv)

    def test_darwin_uses_absolute_osascript_never_the_path_shim(self):
        # REGRESSION: notifications must call /usr/bin/osascript by absolute
        # path. A bare "osascript" would resolve to revertly's own cmdbin guard
        # shim during a session -> flagged SUSPICIOUS -> notify -> infinite loop.
        with mock.patch.dict(os.environ, {}, clear=False), \
             mock.patch("revertly.notify.sys.platform", "darwin"), \
             mock.patch("revertly.notify.os.path.exists", return_value=True), \
             mock.patch("revertly.notify.subprocess.Popen") as popen:
            os.environ.pop("REVERTLY_NO_NOTIFY", None)
            notify.desktop("T", "B")
            argv0 = popen.call_args[0][0][0]
            self.assertEqual(argv0, "/usr/bin/osascript")
            self.assertNotEqual(argv0, "osascript")   # never PATH-resolved

    def test_no_notifier_present_is_silent_noop(self):
        with mock.patch.dict(os.environ, {}, clear=False), \
             mock.patch("revertly.notify.sys.platform", "darwin"), \
             mock.patch("revertly.notify.os.path.exists", return_value=False), \
             mock.patch("revertly.notify.subprocess.Popen") as popen:
            os.environ.pop("REVERTLY_NO_NOTIFY", None)
            notify.desktop("T", "B")           # no osascript on disk -> no call
            popen.assert_not_called()

    def test_never_raises(self):
        with mock.patch.dict(os.environ, {}, clear=False), \
             mock.patch("revertly.notify.shutil.which", return_value="/x"), \
             mock.patch("revertly.notify.subprocess.Popen", side_effect=OSError):
            os.environ.pop("REVERTLY_NO_NOTIFY", None)
            notify.desktop("T", "B")           # must swallow the error


if __name__ == "__main__":
    unittest.main()
