"""Tests for the shared connect loop, the menu bar controller and the root helper."""

import os
import socket
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

os.environ.setdefault("DUCKVPN_HOME", tempfile.mkdtemp())

from duckvpn import controller, ovpn, reputation, session  # noqa: E402
from duckvpn.openvpn import TunnelError  # noqa: E402
from duckvpn.state import State  # noqa: E402

sys.path.insert(0, str(Path(__file__).parent))
from test_duckvpn import servers  # noqa: E402

reputation.BATCH_URL = "http://127.0.0.1:9/"  # stay offline
FAKE = str(Path(__file__).with_name("fake_openvpn.py"))


class FakeTunnel:
    """Fails for configs containing a listed IP; otherwise 'connects'."""

    def __init__(self, config, fail_ips, log):
        self.config, self.fail_ips, self.log = config, fail_ips, log
        self.alive = False

    def start(self, timeout=25):
        self.log.append(("start", self.config))
        if any(ip in self.config for ip in self.fail_ips):
            raise TunnelError("timed out connecting")
        self.alive = True

    def stop(self):
        self.alive = False


class SanitizeTests(unittest.TestCase):
    def test_drops_dangerous_directives_keeps_inline_certs(self):
        cfg = (
            "client\ndev tun\nproto udp\nremote 1.2.3.4 1194\n"
            "script-security 2\nup /tmp/evil.sh\nplugin /tmp/x.so\nca /etc/master.passwd\n"
            "log /etc/sudoers\n# a comment\n<ca>\n-----BEGIN-----\nup inside cert is data\n</ca>\n"
        )
        out = ovpn.sanitize(cfg)
        for bad in ("script-security", "up /tmp", "plugin", "ca /etc", "log /etc"):
            self.assertNotIn(bad, out)
        for good in ("client", "remote 1.2.3.4 1194", "<ca>", "up inside cert is data", "</ca>"):
            self.assertIn(good, out)

    def test_unknown_block_rejected(self):
        with self.assertRaises(ValueError):
            ovpn.sanitize("client\n<connection>\nremote x\n</connection>\n")


class ConnectorTests(unittest.TestCase):
    def setUp(self):
        self.orig_verify = session.verify
        session.verify = lambda code, *a: (True, "OK", {"query": "9.9.9.9", "countryCode": code})
        self.state = State(Path(tempfile.mkdtemp()) / "state.json")

    def tearDown(self):
        session.verify = self.orig_verify

    def run_connector(self, fail_ips, hold):
        jp = [s for s in servers() if s.country_code == "JP"]
        events, log = [], []
        c = session.Connector(
            "JP", jp,
            make_tunnel=lambda cfg: FakeTunnel(cfg, fail_ips, log),
            on_event=lambda kind, **d: events.append((kind, d.get("server").ip if d.get("server") else None)),
            state=self.state,
        )
        return c.run(hold), events

    def test_skips_failures_then_holds(self):
        ok, events = self.run_connector({"10.0.0.1"}, lambda *a: "done")
        self.assertTrue(ok)
        self.assertEqual([e[0] for e in events], ["trying", "failed", "trying", "connected"])
        self.assertIn("10.0.0.1", self.state.bad_ips())
        self.assertEqual(self.state.last["ip"], "10.0.0.2")

    def test_next_moves_on_and_exhausts(self):
        ok, events = self.run_connector(set(), lambda *a: "next")
        self.assertFalse(ok)
        self.assertEqual(events[-1][0], "exhausted")
        self.assertEqual(self.state.bad_ips(), {"10.0.0.1", "10.0.0.2"})


class FakeHelper:
    def __init__(self):
        self.log = []
        self.closed = False

    def request(self, **msg):
        return {"ok": True, "alive": True}

    def tunnel(self, config):
        return FakeTunnel(config, set(), self.log)

    def close(self):
        self.closed = True


class ControllerTests(unittest.TestCase):
    def setUp(self):
        self.orig_verify = session.verify
        session.verify = lambda code, *a: (True, "OK", {"query": "9.9.9.9", "countryCode": code})
        self.helper = FakeHelper()
        self.notes = []
        self.ctl = controller.Controller(lambda: self.helper, notify=lambda t, m: self.notes.append(t))
        self.ctl._servers = servers()
        self.ctl.prefs = dict(controller.DEFAULT_PREFS, country="JP")

    def tearDown(self):
        self.ctl.shutdown(wait=5)
        session.verify = self.orig_verify

    def wait_for(self, phase, timeout=5):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.ctl.snapshot().phase == phase:
                return self.ctl.snapshot()
            time.sleep(0.02)
        self.fail(f"never reached {phase}, stuck at {self.ctl.snapshot()}")

    def test_on_next_off(self):
        self.ctl.turn_on()
        snap = self.wait_for("on")
        first = snap.server.ip
        self.assertEqual(snap.exit_ip, "9.9.9.9")
        self.ctl.next_server()
        deadline = time.monotonic() + 5
        while self.ctl.snapshot().server is None or self.ctl.snapshot().server.ip == first:
            self.assertLess(time.monotonic(), deadline)
            time.sleep(0.02)
        self.ctl.turn_off()
        self.wait_for("off")
        self.assertIn("Connected through Japan", self.notes)

    def test_country_change_reconnects(self):
        self.ctl.turn_on()
        self.wait_for("on")
        self.ctl.set_country("US")
        deadline = time.monotonic() + 5
        while True:
            snap = self.ctl.snapshot()
            if snap.phase == "on" and snap.server and snap.server.country_code == "US":
                break
            self.assertLess(time.monotonic(), deadline, snap)
            time.sleep(0.02)

    def test_helper_refused(self):
        def refuse():
            raise TunnelError("password prompt cancelled")

        ctl = controller.Controller(refuse, notify=lambda t, m: self.notes.append(m))
        ctl.turn_on()
        ctl._worker.join(5)
        self.assertEqual(ctl.snapshot().phase, "off")
        self.assertIn("password prompt cancelled", self.notes)


@unittest.skipUnless(hasattr(socket, "AF_UNIX") and os.name == "posix", "Unix sockets only")
class HelperTests(unittest.TestCase):
    def test_protocol_roundtrip(self):
        from duckvpn import helper

        path = os.path.join(tempfile.mkdtemp(), "h.sock")
        t = threading.Thread(target=helper.serve, args=(path, os.getuid(), FAKE), daemon=True)
        t.start()
        client = helper.HelperClient.connect(Path(path), wait=5)
        self.assertEqual(oct(os.stat(path).st_mode & 0o777), "0o600")

        bad = client.tunnel("client\n<connection>\n</connection>\n")
        with self.assertRaises(TunnelError):
            bad.start(timeout=5)

        tun = client.tunnel("client\nremote 1.2.3.4 1194\n")
        tun.start(timeout=10)
        self.assertTrue(tun.alive)
        tun.stop()
        self.assertFalse(tun.alive)

        tun.start(timeout=10)
        client.close()  # app quits -> helper must tear down and exit
        t.join(10)
        self.assertFalse(t.is_alive())
        self.assertFalse(os.path.exists(path))


if __name__ == "__main__":
    unittest.main()
