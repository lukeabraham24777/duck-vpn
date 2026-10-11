import base64
import http.server
import os
import tempfile
import threading
import unittest
from pathlib import Path

_TMP = tempfile.mkdtemp()
os.environ["DUCKVPN_HOME"] = _TMP

from duckvpn import cli, ovpn, probe, vpngate  # noqa: E402
from duckvpn.state import State  # noqa: E402


def _config(ip, port=1195, proto="udp"):
    text = f"dev tun\nproto {proto}\nremote {ip} {port}\ncipher AES-128-CBC\n<ca>\nX\n</ca>\n"
    return base64.b64encode(text.encode()).decode()


HEADER = (
    "#HostName,IP,Score,Ping,Speed,CountryLong,CountryShort,NumVpnSessions,Uptime,"
    "TotalUsers,TotalTraffic,LogType,Operator,Message,OpenVPN_ConfigData_Base64"
)
ROWS = [
    f"public-vpn-1,10.0.0.1,500,12,90000000,Japan,JP,5,3600000,10,10,2weeks,Op A,,{_config('10.0.0.1')}",
    f"public-vpn-2,10.0.0.2,900,-,50000000,Japan,JP,5,3600000,10,10,2weeks,Op B,,{_config('10.0.0.2', 443, 'tcp')}",
    f"us-box,10.0.0.3,700,30,20000000,United States,US,1,1000,1,1,2weeks,Op C,,{_config('10.0.0.3')}",
    "broken,10.0.0.4,100,1,1,Korea Republic of,KR,1,1,1,1,2weeks,Op D,,!!notbase64!!",
    "noconfig,10.0.0.5,100,1,1,Korea Republic of,KR,1,1,1,1,2weeks,Op E,,",
]
SAMPLE = "*vpn_servers\n" + HEADER + "\n" + "\n".join(ROWS) + "\n*\n"


def servers():
    return list(vpngate.parse(SAMPLE))


class ParseTests(unittest.TestCase):
    def test_parses_valid_rows_and_skips_bad_ones(self):
        s = servers()
        self.assertEqual([x.ip for x in s], ["10.0.0.1", "10.0.0.2", "10.0.0.3"])
        jp2 = s[1]
        self.assertIsNone(jp2.ping)
        self.assertEqual((jp2.proto, jp2.port), ("tcp", 443))
        self.assertEqual(jp2.speed_mbps, 50.0)
        self.assertIn("remote 10.0.0.2 443", jp2.config)

    def test_countries_and_resolve(self):
        s = servers()
        self.assertEqual(vpngate.countries(s)[0], ("JP", "Japan", 2))
        self.assertEqual(vpngate.resolve_country("jp", s), "JP")
        self.assertEqual(vpngate.resolve_country("Japan", s), "JP")
        self.assertEqual(vpngate.resolve_country("united", s), "US")
        self.assertIsNone(vpngate.resolve_country("france", s))

    def test_roundtrip_dict(self):
        s = servers()[0]
        d = s.to_dict()
        d["unknown_future_field"] = 1
        self.assertEqual(vpngate.Server.from_dict(d), s)


class RankTests(unittest.TestCase):
    def test_home_ips_beat_higher_score(self):
        a, b, _ = servers()
        a.hosting, a.proxy = False, False  # home IP, score 500
        b.hosting, b.proxy = True, False   # datacentre, score 900
        self.assertEqual(vpngate.rank([b, a])[0].ip, a.ip)

    def test_unknown_sorted_by_score(self):
        a, b, _ = servers()
        self.assertEqual([s.ip for s in vpngate.rank([a, b])], [b.ip, a.ip])

    def test_known_good_first_and_bad_last(self):
        a, b, c = servers()
        ranked = vpngate.rank([a, b, c], known_good=[c.ip], known_bad=[b.ip])
        self.assertEqual([s.ip for s in ranked], [c.ip, a.ip, b.ip])


class OvpnTests(unittest.TestCase):
    def test_desktop_adds_legacy_ciphers_and_routing(self):
        text = ovpn.for_desktop(servers()[0])
        self.assertIn("data-ciphers-fallback AES-128-CBC", text)
        self.assertIn("redirect-gateway def1", text)
        self.assertIn("remote 10.0.0.1 1195", text)

    def test_mobile_keeps_original(self):
        text = ovpn.for_mobile(servers()[0])
        self.assertNotIn("data-ciphers", text)
        self.assertTrue(text.startswith("# duck-vpn:"))

    def test_filename_is_safe(self):
        s = servers()[0]
        s.host = "weird/host name!"
        self.assertEqual(ovpn.filename(s), "duck-jp-weird-host-name.ovpn")


class StateTests(unittest.TestCase):
    def test_good_and_bad_persist(self):
        path = Path(_TMP) / "state-test.json"
        a, b, _ = servers()
        st = State(path)
        st.mark_good(a)
        st.mark_bad(b)
        st.save()
        st2 = State(path)
        self.assertEqual([s.ip for s in st2.good_servers("JP")], [a.ip])
        self.assertEqual(st2.bad_ips(), {b.ip})
        self.assertEqual(st2.last["ip"], a.ip)
        st2.mark_bad(a)
        self.assertEqual(st2.good_servers("JP"), [])


class _Handler(http.server.BaseHTTPRequestHandler):
    pages = {
        "/ok": (200, "<h1>Welcome to the show</h1>"),
        "/geo": (200, "Sorry, this video is not available in your country."),
        "/forbidden": (403, "nope"),
    }

    def do_GET(self):
        code, body = self.pages.get(self.path, (404, ""))
        self.send_response(code)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(body.encode())

    def log_message(self, *args):
        pass


class ProbeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = http.server.HTTPServer(("127.0.0.1", 0), _Handler)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def test_ok(self):
        self.assertEqual(probe.check_site(self.base + "/ok"), (True, "loaded"))

    def test_expect(self):
        self.assertTrue(probe.check_site(self.base + "/ok", expect="the show")[0])
        ok, reason = probe.check_site(self.base + "/ok", expect="subscribe")
        self.assertFalse(ok)
        self.assertIn("subscribe", reason)

    def test_block_phrase(self):
        ok, reason = probe.check_site(self.base + "/geo")
        self.assertFalse(ok)
        self.assertIn("not available in your country", reason)

    def test_custom_reject(self):
        self.assertFalse(probe.check_site(self.base + "/ok", reject=["welcome"])[0])

    def test_http_error(self):
        self.assertEqual(probe.check_site(self.base + "/forbidden"), (False, "HTTP 403"))


class CliTests(unittest.TestCase):
    def test_export_writes_files(self):
        (Path(_TMP) / "servers.csv").write_text(SAMPLE, encoding="utf-8")
        out = Path(_TMP) / "export"
        # Point reputation lookups at nothing so the test stays offline.
        from duckvpn import reputation

        orig = reputation.BATCH_URL
        reputation.BATCH_URL = "http://127.0.0.1:9/"
        try:
            rc = cli.main(["export", "japan", "--count", "2", "--out", str(out)])
        finally:
            reputation.BATCH_URL = orig
        self.assertEqual(rc, 0)
        self.assertEqual(len(list(out.glob("*.ovpn"))), 2)

    def test_parser(self):
        args = cli.build_parser().parse_args(["connect", "us", "--test", "https://x", "--reject", "a", "--reject", "b"])
        self.assertEqual(args.reject, ["a", "b"])
        self.assertEqual(args.max_tries, 15)
        self.assertEqual(cli.build_parser().prog, "duck-vpn")
        self.assertTrue(cli.build_parser().parse_args(["menubar", "--foreground"]).foreground)


if __name__ == "__main__":
    unittest.main()
