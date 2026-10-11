"""Checks run through a live tunnel: where do we exit, and does the site load?"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Iterable, Optional

from . import BROWSER_USER_AGENT, USER_AGENT

EXIT_INFO_URL = "http://ip-api.com/json/?fields=status,query,countryCode,isp,proxy,hosting"

# Phrases geo-blocked pages commonly show. Matched case-insensitively.
DEFAULT_BLOCK_PHRASES = (
    "not available in your country",
    "not available in your region",
    "not available in your location",
    "unavailable in your country",
    "unavailable in your region",
    "using a vpn",
    "using an unblocker",
    "vpn or proxy",
    "proxy or vpn",
)

MAX_BODY = 2_000_000


def exit_info(timeout: float = 10) -> Optional[dict]:
    """What the internet sees: our exit IP, country and whether it's flagged."""
    req = urllib.request.Request(EXIT_INFO_URL, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.load(resp)
    except (OSError, ValueError):
        return None
    return data if data.get("status") == "success" else None


def check_site(
    url: str,
    expect: Optional[str] = None,
    reject: Iterable[str] = (),
    timeout: float = 15,
) -> tuple[bool, str]:
    """Fetch url and decide whether it looks unblocked.

    Fails on HTTP errors, on any block phrase in the page, or when `expect`
    is given and missing from the page.
    """
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": BROWSER_USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
            "Accept-Language": "en;q=0.8,*;q=0.5",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read(MAX_BODY).decode("utf-8", "replace").lower()
    except urllib.error.HTTPError as e:
        return False, f"HTTP {e.code}"
    except OSError as e:
        return False, f"could not load ({getattr(e, 'reason', e)})"
    for phrase in (*DEFAULT_BLOCK_PHRASES, *reject):
        if phrase.lower() in body:
            return False, f'page says "{phrase}"'
    if expect and expect.lower() not in body:
        return False, f'"{expect}" not found on page'
    return True, "loaded"
