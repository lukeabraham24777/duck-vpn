"""The `duck-vpn` command."""

from __future__ import annotations

import argparse
import sys
import threading
from pathlib import Path

from . import __version__, ovpn, session, vpngate
from .openvpn import Tunnel, TunnelError, find_openvpn, privilege_prefix
from .state import State
from .vpngate import FetchError, Server


def _load(args) -> list[Server]:
    try:
        return vpngate.fetch(max_age=0 if args.refresh else vpngate.CACHE_TTL)
    except FetchError as e:
        sys.exit(f"error: {e}")


def _country(query: str, servers: list[Server]) -> str:
    code = vpngate.resolve_country(query, servers)
    if not code:
        available = ", ".join(c for c, _, _ in vpngate.countries(servers))
        sys.exit(f"error: no VPN Gate servers for '{query}' right now.\nAvailable: {available}")
    return code


def cmd_countries(args) -> int:
    servers = _load(args)
    print(f"{'CODE':<6}{'COUNTRY':<28}SERVERS")
    for code, name, count in vpngate.countries(servers):
        print(f"{code:<6}{name:<28}{count}")
    return 0


def cmd_list(args) -> int:
    servers = _load(args)
    code = _country(args.country, servers)
    ranked = session.candidates(code, servers, State())[: args.limit]
    print(f"{'#':<4}{'HOST':<22}{'IP':<17}{'TYPE':<12}{'MBPS':>6}{'PING':>6}{'SCORE':>9}  ISP")
    for i, s in enumerate(ranked, 1):
        ping = "-" if s.ping is None else s.ping
        print(f"{i:<4}{s.host[:21]:<22}{s.ip:<17}{s.label:<12}{s.speed_mbps:>6}{ping:>6}{s.score:>9}  {s.isp[:30]}")
    return 0


def cmd_connect(args) -> int:
    servers = _load(args)
    code = _country(args.country, servers)
    state = State()
    ranked = session.candidates(code, servers, state, home_only=args.home_only)[: args.max_tries]
    if not ranked:
        sys.exit("error: no matching servers. Try without --home-only or another country.")

    binary = find_openvpn()
    if not binary:
        sys.exit(
            "error: OpenVPN not found.\n"
            "  Windows: install from https://openvpn.net/community-downloads/\n"
            "  macOS:   brew install openvpn"
        )
    try:
        prefix = privilege_prefix()
    except TunnelError as e:
        sys.exit(f"error: {e}")

    target = f" and testing {args.test}" if args.test else ""
    print(f"Trying up to {len(ranked)} servers in {ranked[0].country}{target}.\n")

    def on_event(kind, server=None, index=0, total=0, reason="", info=None):
        if kind == "trying":
            print(f"[{index}/{total}] {server.host} {server.ip} ({server.label}) ... ", end="", flush=True)
        elif kind == "failed" and reason != "skipped":
            print(reason)
        elif kind == "connected":
            flagged = " (exit IP flagged as proxy/datacentre)" if info.get("proxy") or info.get("hosting") else ""
            print(f"OK, exit IP {info.get('query')}{flagged}")
        elif kind == "exhausted":
            print("\nNo working server found. VPN Gate's list changes constantly; try again later,")
            print("or try a neighbouring country (`duck-vpn countries`).")

    connector = session.Connector(
        code,
        ranked,
        make_tunnel=lambda cfg: Tunnel(cfg, binary, prefix, verbose=args.verbose),
        on_event=on_event,
        state=state,
        test=args.test,
        expect=args.expect,
        reject=args.reject,
        timeout=args.timeout,
    )
    try:
        return 0 if connector.run(_hold) else 1
    except KeyboardInterrupt:
        print("\nDisconnected.")
        return 130


def _hold(server: Server, tunnel: Tunnel, info: dict) -> str:
    """Keep the tunnel up until the user is done."""
    dropped = threading.Event()

    def on_drop():
        dropped.set()
        print("\n! The tunnel dropped. Press Enter to try the next server.")

    tunnel.on_drop = on_drop
    print(
        f"\nYou're connected through {server.country}. Leave this window open and use your\n"
        "browser as normal; https://ipinfo.io should now show the new country.\n"
        "  Enter      disconnect and quit\n"
        "  n + Enter  the site is still blocked, try the next server"
    )
    try:
        answer = input("> ").strip().lower()
    except EOFError:
        answer = ""
    if dropped.is_set() or answer in ("n", "next"):
        return "next"
    return "done"


def cmd_export(args) -> int:
    state = State()
    if args.last:
        if not state.last:
            sys.exit("error: no server has worked yet. Run `duck-vpn connect` first.")
        chosen = [Server.from_dict(state.last)]
    else:
        if not args.country:
            sys.exit("error: give a country, or use --last")
        servers = _load(args)
        code = _country(args.country, servers)
        chosen = session.candidates(code, servers, state)[: args.count]

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    for s in chosen:
        path = out / ovpn.filename(s)
        path.write_text(ovpn.for_mobile(s), encoding="utf-8")
        print(f"wrote {path}  ({s.country}, {s.label})")
    print(
        "\nOpen these in OpenVPN Connect (iPhone: AirDrop the file, then 'Open in OpenVPN'),"
        "\nTunnelblick (Mac) or OpenVPN GUI (Windows). If OpenVPN Connect refuses to connect,"
        "\nset Settings > Security Level to 'Insecure': VPN Gate relays use older ciphers."
    )
    return 0


def cmd_menubar(args) -> int:
    if sys.platform != "darwin":
        sys.exit("error: the menu bar app is macOS-only.")
    from . import menubar

    if args.autostart:
        path = menubar.set_autostart(args.autostart == "on")
        print(f"Start at login {'enabled' if args.autostart == 'on' else 'disabled'} ({path})")
        return 0
    if args.foreground:
        return menubar.run()
    log = menubar.launch_background()
    print(f"duck-vpn is running in your menu bar (look for the duck). Log: {log}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="duck-vpn",
        description="Find a free VPN Gate server in another country that gets past geo-blocks.",
    )
    p.add_argument("--version", action="version", version=f"duck-vpn {__version__}")
    p.add_argument("--refresh", action="store_true", help="re-download the server list now")
    sub = p.add_subparsers(dest="command", required=True)

    sp = sub.add_parser("countries", help="list countries with servers right now")
    sp.set_defaults(func=cmd_countries)

    sp = sub.add_parser("list", help="show ranked servers for a country")
    sp.add_argument("country", help="country code or name, e.g. jp or japan")
    sp.add_argument("--limit", type=int, default=20)
    sp.set_defaults(func=cmd_list)

    sp = sub.add_parser("connect", help="connect, test, and stay connected to the first server that works")
    sp.add_argument("country", help="country code or name, e.g. us or 'united states'")
    sp.add_argument("--test", metavar="URL", help="site that must load through the tunnel")
    sp.add_argument("--expect", metavar="TEXT", help="text that must appear on the test page")
    sp.add_argument("--reject", metavar="TEXT", action="append", help="extra text that means 'blocked' (repeatable)")
    sp.add_argument("--max-tries", type=int, default=15, help="servers to try (default 15)")
    sp.add_argument("--timeout", type=float, default=25, help="seconds to wait per server (default 25)")
    sp.add_argument("--home-only", action="store_true", help="only try servers on home (residential) IPs")
    sp.add_argument("-v", "--verbose", action="store_true", help="show OpenVPN output")
    sp.set_defaults(func=cmd_connect)

    sp = sub.add_parser("export", help="save .ovpn files for your phone or a VPN app")
    sp.add_argument("country", nargs="?", help="country code or name")
    sp.add_argument("--last", action="store_true", help="export the last server that worked")
    sp.add_argument("--count", type=int, default=3, help="how many servers to export (default 3)")
    sp.add_argument("--out", default=".", help="output folder (default: current folder)")
    sp.set_defaults(func=cmd_export)
    sp = sub.add_parser("menubar", help="run the macOS menu bar app")
    sp.add_argument("--foreground", action="store_true", help="stay attached to this terminal")
    sp.add_argument("--autostart", choices=["on", "off"], help="start the menu bar app when you log in")
    sp.set_defaults(func=cmd_menubar)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)
