"""Where duck-vpn keeps its cache and state."""

from __future__ import annotations

import os
import sys
from pathlib import Path


def data_dir() -> Path:
    override = os.environ.get("DUCKVPN_HOME")
    if override:
        base = Path(override)
    elif sys.platform == "win32":
        base = Path(os.environ.get("APPDATA", Path.home())) / "duck-vpn"
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support" / "duck-vpn"
    else:
        base = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share")) / "duck-vpn"
    base.mkdir(parents=True, exist_ok=True)
    return base
