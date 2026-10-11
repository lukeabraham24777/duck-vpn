"""Look up whether server IPs are flagged as datacentre or proxy addresses.

Streaming services block IPs that IP-intelligence databases mark as hosting
or proxy. ip-api.com exposes the same kind of flags for free (non-commercial
use, 15 batch requests per minute), so we use it to try likely-residential
servers first. Results are cached for a day.
"""

from __future__ import annotations

import json
import time
import urllib.request
from typing import Iterable

from . import USER_AGENT, paths
from .vpngate import Server

BATCH_URL = "http://ip-api.com/batch?fields=status,query,countryCode,isp,mobile,proxy,hosting"
BATCH_SIZE = 100
CACHE_TTL = 24 * 3600


def _cache_path():
    return paths.data_dir() / "reputation.json"


def _load_cache() -> dict:
    try:
        data = json.loads(_cache_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    now = time.time()
    return {ip: v for ip, v in data.items() if now - v.get("ts", 0) < CACHE_TTL}


def lookup(ips: Iterable[str], timeout: float = 15) -> dict[str, dict]:
    cache = _load_cache()
    missing = [ip for ip in dict.fromkeys(ips) if ip not in cache]
    for i in range(0, len(missing), BATCH_SIZE):
        chunk = missing[i : i + BATCH_SIZE]
        req = urllib.request.Request(
            BATCH_URL,
            data=json.dumps(chunk).encode(),
            headers={"Content-Type": "application/json", "User-Agent": USER_AGENT},
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                results = json.load(resp)
        except (OSError, ValueError):
            break  # Reputation is a nice-to-have; carry on without it.
        now = time.time()
        for item in results:
            if item.get("status") == "success":
                item["ts"] = now
                cache[item["query"]] = item
    try:
        _cache_path().write_text(json.dumps(cache), encoding="utf-8")
    except OSError:
        pass
    return cache


def annotate(servers: list[Server]) -> list[Server]:
    info = lookup(s.ip for s in servers)
    for s in servers:
        i = info.get(s.ip)
        if i:
            s.hosting = bool(i.get("hosting"))
            s.proxy = bool(i.get("proxy"))
            s.isp = i.get("isp", "")
    return servers
