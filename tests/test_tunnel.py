import sys
import time
import unittest
from pathlib import Path

from duckvpn.openvpn import Tunnel, TunnelError

FAKE = str(Path(__file__).with_name("fake_openvpn.py"))


def tunnel(config):
    # prefix=[python] so the fake script runs the same way on every OS.
    return Tunnel(config, FAKE, prefix=[sys.executable])


class TunnelTests(unittest.TestCase):
    def test_start_and_clean_stop(self):
        t = tunnel("remote 1.2.3.4 1194\n")
        t.start(timeout=10)
        self.assertTrue(t.alive)
        start = time.monotonic()
        t.stop()
        self.assertFalse(t.alive)
        # Stopped via the management port, not by waiting out the kill timeout.
        self.assertLess(time.monotonic() - start, 5)
        self.assertTrue(any("SIGTERM received" in line for line in t.log))

    def test_fatal_error_is_reported(self):
        t = tunnel("FAIL\n")
        with self.assertRaises(TunnelError) as cm:
            t.start(timeout=10)
        self.assertIn("Exiting due to fatal error", str(cm.exception))

    def test_drop_callback(self):
        t = tunnel("remote 1.2.3.4 1194\n")
        t.start(timeout=10)
        dropped = []
        t.on_drop = lambda: dropped.append(True)
        t._proc.kill()
        t._proc.wait()
        time.sleep(0.5)
        self.assertEqual(dropped, [True])
        t.stop()


if __name__ == "__main__":
    unittest.main()
