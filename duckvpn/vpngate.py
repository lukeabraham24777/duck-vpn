"""Fetch, parse and rank the public VPN Gate server list.

VPN Gate (https://www.vpngate.net) is a volunteer relay network run as a
research project by the University of Tsukuba. Its server list is a CSV with
one row per relay, including a base64-encoded OpenVPN config.
"""

from __future__ import annotations

import base64
import binascii
import csv
import dataclasses
import os
import re
import time
import urllib.request
from dataclasses import dataclass, field
from typing import Iterable, Iterator, Optional

from . import USER_AGENT, paths

API_URL = os.environ.get("DUCKVPN_LIST_URL", "http://www.vpngate.net/api/iphone/")
CACHE_TTL = 15 * 60


class FetchError(Exception):
    pass


@dataclass
class Server:
    host: str
    ip: str
    score: int
    ping: Optional[int]
    speed: int  # bits per second
    country: str
    country_code: str
    sessions: int
    uptime_ms: int
    operator: str
    config: str = field(repr=False)
    # Filled in by reputation.annotate().
    hosting: Optional[bool] = None
    proxy: Optional[bool] = None
    isp: str = ""

    @property
    def residential(self) -> Optional[bool]:
        """True if the IP looks like a home connection, None if unknown."""
        if self.hosting is None and self.proxy is None:
            return None
        return not (self.hosting or self.proxy)

    @property
    def label(self) -> str:
        return {True: "home IP", False: "flagged IP", None: "unknown IP"}[self.residential]

    @property
    def proto(self) -> str:
        m = re.search(r"^\s*proto\s+(\w+)", self.config, re.M)
        return m.group(1).lower() if m else "udp"

    @property
    def port(self) -> Optional[int]:
        m = re.search(r"^\s*remote\s+\S+\s+(\d+)", self.config, re.M)
        return int(m.group(1)) if m else None

    @property
    def speed_mbps(self) -> float:
        return round(self.speed / 1_000_000, 1)

    def to_dict(self) -> dict:
        return dataclasses.asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Server":
        names = {f.name for f in dataclasses.fields(cls)}
        return cls(**{k: v for k, v in d.items() if k in names})


def _int(value: str | None, default: Optional[int] = 0) -> Optional[int]:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


def parse(text: str) -> Iterator[Server]:
    lines = [ln for ln in text.splitlines() if ln.strip() and not ln.startswith("*")]
    if not lines:
        return
    if lines[0].startswith("#"):
        lines[0] = lines[0][1:]
    for row in csv.DictReader(lines):
        b64 = (row.get("OpenVPN_ConfigData_Base64") or "").strip()
        if not b64 or not row.get("IP"):
            continue
        try:
            config = base64.b64decode(b64).decode("utf-8", "replace")
        except (binascii.Error, ValueError):
            continue
        if "remote " not in config:
            continue
        yield Server(
            host=row.get("HostName", ""),
            ip=row["IP"],
            score=_int(row.get("Score")) or 0,
            ping=_int(row.get("Ping"), None),
            speed=_int(row.get("Speed")) or 0,
            country=row.get("CountryLong", ""),
            country_code=(row.get("CountryShort") or "").upper(),
            sessions=_int(row.get("NumVpnSessions")) or 0,
            uptime_ms=_int(row.get("Uptime")) or 0,
            operator=row.get("Operator", ""),
            config=config,
        )


def fetch(url: str = API_URL, max_age: float = CACHE_TTL, timeout: float = 30) -> list[Server]:
    """Return the server list, reusing a cached copy younger than max_age seconds."""
    cache = paths.data_dir() / "servers.csv"
    if max_age and cache.exists() and time.time() - cache.stat().st_mtime < max_age:
        return list(parse(cache.read_text(encoding="utf-8")))
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            text = resp.read().decode("utf-8", "replace")
    except OSError as e:
        if cache.exists():
            return list(parse(cache.read_text(encoding="utf-8")))
        raise FetchError(f"could not download the VPN Gate list from {url}: {e}") from e
    if "*vpn_servers" not in text:
        raise FetchError(f"unexpected response from {url}")
    cache.write_text(text, encoding="utf-8")
    return list(parse(text))


def countries(servers: Iterable[Server]) -> list[tuple[str, str, int]]:
    """(code, name, count) sorted by count, most servers first."""
    counts: dict[str, list] = {}
    for s in servers:
        entry = counts.setdefault(s.country_code, [s.country, 0])
        entry[1] += 1
    return sorted(((c, n, k) for c, (n, k) in counts.items()), key=lambda t: (-t[2], t[0]))


def resolve_country(query: str, servers: Iterable[Server]) -> Optional[str]:
    """Accept a code ("jp") or a name ("japan", "united states") and return the code."""
    q = query.strip().lower()
    by_name = {}
    for code, name, _ in countries(servers):
        if code.lower() == q:
            return code
        by_name[name.lower()] = code
    if q in by_name:
        return by_name[q]
    matches = [code for name, code in by_name.items() if name.startswith(q)]
    return matches[0] if len(matches) == 1 else None


def rank(servers: Iterable[Server], known_good: Iterable[str] = (), known_bad: Iterable[str] = ()) -> list[Server]:
    """Order servers by how likely they are to get past geo-blocks.

    Servers that worked before come first, then home IPs, then unknown, then
    IPs flagged as datacentre/proxy. Within a group, higher VPN Gate score
    wins. Servers that recently failed go to the end.
    """
    good = list(known_good)
    bad = set(known_bad)

    def key(s: Server):
        worked = good.index(s.ip) if s.ip in good else len(good)
        residential = {True: 0, None: 1, False: 2}[s.residential]
        return (s.ip in bad, worked, residential, -s.score)

    return sorted(servers, key=key)
