"""Remember which servers worked (or failed) so the next run is faster."""

from __future__ import annotations

import json
import time

from . import paths
from .vpngate import Server

BAD_TTL = 24 * 3600
GOOD_KEEP = 5


class State:
    def __init__(self, path=None):
        self.path = path or paths.data_dir() / "state.json"
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        self.good: dict[str, list[dict]] = data.get("good", {})
        self.bad: dict[str, float] = data.get("bad", {})
        self.last: dict | None = data.get("last")

    def save(self) -> None:
        now = time.time()
        self.bad = {ip: ts for ip, ts in self.bad.items() if now - ts < BAD_TTL}
        data = {"good": self.good, "bad": self.bad, "last": self.last}
        self.path.write_text(json.dumps(data, indent=1), encoding="utf-8")

    def good_servers(self, country_code: str) -> list[Server]:
        return [Server.from_dict(d) for d in self.good.get(country_code, [])]

    def bad_ips(self) -> set[str]:
        now = time.time()
        return {ip for ip, ts in self.bad.items() if now - ts < BAD_TTL}

    def mark_good(self, server: Server) -> None:
        entries = [d for d in self.good.get(server.country_code, []) if d["ip"] != server.ip]
        self.good[server.country_code] = [server.to_dict()] + entries[: GOOD_KEEP - 1]
        self.bad.pop(server.ip, None)
        self.last = server.to_dict()

    def mark_bad(self, server: Server) -> None:
        self.bad[server.ip] = time.time()
        entries = self.good.get(server.country_code, [])
        self.good[server.country_code] = [d for d in entries if d["ip"] != server.ip]
