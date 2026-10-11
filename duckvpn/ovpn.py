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


# Directives a VPN Gate client config legitimately needs. Anything else is
# dropped: the list comes over plain HTTP and OpenVPN runs as root, so a
# tampered config must not be able to run scripts, load plugins or touch files.
ALLOWED_DIRECTIVES = {
    "client", "dev", "dev-type", "proto", "remote", "port", "rport", "nobind", "float",
    "cipher", "auth", "data-ciphers", "data-ciphers-fallback", "tls-cipher", "tls-client",
    "tls-version-min", "remote-cert-tls", "ns-cert-type", "verify-x509-name", "key-direction",
    "resolv-retry", "persist-key", "persist-tun", "keepalive", "ping", "ping-restart",
    "reneg-sec", "tun-mtu", "mssfix", "fragment", "sndbuf", "rcvbuf", "comp-lzo", "compress",
    "redirect-gateway", "pull", "auth-nocache", "explicit-exit-notify", "verb", "mute",
    "connect-retry", "connect-timeout", "server-poll-timeout",
}
INLINE_BLOCKS = {"ca", "cert", "key", "tls-auth", "tls-crypt", "extra-certs"}


def sanitize(config: str) -> str:
    """Keep only allow-listed directives and inline certificate blocks."""
    out = []
    block = None
    for line in config.splitlines():
        stripped = line.strip()
        if block:
            out.append(line)
            if stripped.lower() == f"</{block}>":
                block = None
            continue
        if not stripped or stripped[0] in "#;":
            out.append(line)
            continue
        m = re.fullmatch(r"<([a-z-]+)>", stripped.lower())
        if m:
            if m.group(1) in INLINE_BLOCKS:
                block = m.group(1)
                out.append(line)
                continue
            raise ValueError(f"config contains a disallowed <{m.group(1)}> block")
        if stripped.split()[0].lower() in ALLOWED_DIRECTIVES:
            out.append(line)
    return "\n".join(out) + "\n"


def _header(server: Server) -> str:
    return (
        f"# duck-vpn: {server.host} ({server.country}, {server.label})\n"
        f"# Relay operated by a VPN Gate volunteer: {server.operator}\n"
    )


def for_desktop(server: Server) -> str:
    """Config for the OpenVPN 2.5+ command-line client that duck-vpn drives."""
    return _header(server) + sanitize(server.config).rstrip() + "\n" + DESKTOP_EXTRA


def for_mobile(server: Server) -> str:
    """Config for OpenVPN Connect (iOS/Android/desktop) and Tunnelblick.

    These apps reject some command-line-only options, so none are added;
    legacy ciphers are enabled in the app's settings instead.
    """
    return _header(server) + sanitize(server.config).rstrip() + "\n"


def filename(server: Server) -> str:
    host = re.sub(r"[^A-Za-z0-9_-]+", "-", server.host).strip("-") or server.ip.replace(".", "-")
    return f"duck-{server.country_code.lower()}-{host}.ovpn"
