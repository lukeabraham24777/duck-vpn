"""Turn VPN Gate configs into files that modern OpenVPN clients accept."""

from __future__ import annotations

import re

from .vpngate import Server

# VPN Gate relays run SoftEther with older crypto (AES-128-CBC, SHA1-signed
# certificates). OpenVPN 2.5+ on OpenSSL 3 refuses these unless told otherwise.
DESKTOP_EXTRA = """
# --- added by duck-vpn ---
data-ciphers AES-256-GCM:AES-128-GCM:CHACHA20-POLY1305:AES-128-CBC
data-ciphers-fallback AES-128-CBC
tls-cipher DEFAULT:@SECLEVEL=0
# Send all traffic through the tunnel.
redirect-gateway def1
"""


def _header(server: Server) -> str:
    return (
        f"# duck-vpn: {server.host} ({server.country}, {server.label})\n"
        f"# Relay operated by a VPN Gate volunteer: {server.operator}\n"
    )


def for_desktop(server: Server) -> str:
    """Config for the OpenVPN 2.5+ command-line client that `duck connect` drives."""
    return _header(server) + server.config.rstrip() + "\n" + DESKTOP_EXTRA


def for_mobile(server: Server) -> str:
    """Config for OpenVPN Connect (iOS/Android/desktop) and Tunnelblick.

    These apps reject some command-line-only options, so the original config
    is kept as-is; legacy ciphers are enabled in the app's settings instead.
    """
    return _header(server) + server.config.rstrip() + "\n"


def filename(server: Server) -> str:
    host = re.sub(r"[^A-Za-z0-9_-]+", "-", server.host).strip("-") or server.ip.replace(".", "-")
    return f"duck-{server.country_code.lower()}-{host}.ovpn"
