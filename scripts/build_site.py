"""Build the static server picker for phones (published to GitHub Pages).

Usage: python scripts/build_site.py OUTPUT_DIR

Writes OUTPUT_DIR/index.html, servers.json and one .ovpn file per server.
A phone can't test servers itself, so the page ranks them by the same
home-IP heuristic `duck connect` uses.
"""

from __future__ import annotations

import json
import shutil
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from duckvpn import ovpn, reputation, vpngate  # noqa: E402

WEB_DIR = Path(__file__).resolve().parent.parent / "web"


def build(out: Path) -> int:
    servers = vpngate.fetch(max_age=0)
    reputation.annotate(servers)
    ranked = vpngate.rank(servers)

    (out / "ovpn").mkdir(parents=True, exist_ok=True)
    entries = []
    for s in ranked:
        name = ovpn.filename(s)
        (out / "ovpn" / name).write_text(ovpn.for_mobile(s), encoding="utf-8")
        entries.append(
            {
                "host": s.host,
                "ip": s.ip,
                "country": s.country,
                "cc": s.country_code,
                "score": s.score,
                "ping": s.ping,
                "mbps": s.speed_mbps,
                "uptimeDays": round(s.uptime_ms / 86_400_000, 1),
                "residential": s.residential,
                "isp": s.isp,
                "proto": s.proto,
                "port": s.port,
                "file": f"ovpn/{name}",
            }
        )

    data = {"generated": int(time.time()), "servers": entries}
    (out / "servers.json").write_text(json.dumps(data), encoding="utf-8")
    shutil.copy(WEB_DIR / "index.html", out / "index.html")
    print(f"built {len(entries)} servers in {len(vpngate.countries(servers))} countries -> {out}")
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    sys.exit(build(Path(sys.argv[1])))
