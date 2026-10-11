"""The `duck` command."""

from __future__ import annotations

import argparse
import sys
import threading
from pathlib import Path

from . import __version__, ovpn, probe, reputation, vpngate
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


def _candidates(code: str, servers: list[Server], state: State) -> list[Server]:
    fresh = [s for s in servers if s.country_code == code]
    # Servers that worked before but dropped off the current list may still be up.
    fresh_ips = {s.ip for s in fresh}
    remembered = state.good_servers(code)
    pool = fresh + [s for s in remembered if s.ip not in fresh_ips]
    reputation.annotate(pool)
    return vpngate.rank(pool, known_good=[s.ip for s in remembered], known_bad=state.bad_ips())


def cmd_countries(args) -> int:
    servers = _load(args)
    print(f"{'CODE':<6}{'COUNTRY':<28}SERVERS")
    for code, name, count in vpngate.countries(servers):
        print(f"{code:<6}{name:<28}{count}")
    return 0


def cmd_list(args) -> int:
    servers = _load(args)
    code = _country(args.country, servers)
    ranked = _candidates(code, servers, State())[: args.limit]
    print(f"{'#':<4}{'HOST':<22}{'IP':<17}{'TYPE':<12}{'MBPS':>6}{'PING':>6}{'SCORE':>9}  ISP")
    for i, s in enumerate(ranked, 1):
        ping = "-" if s.ping is None else s.ping
        print(f"{i:<4}{s.host[:21]:<22}{s.ip:<17}{s.label:<12}{s.speed_mbps:>6}{ping:>6}{s.score:>9}  {s.isp[:30]}")
    return 0


def cmd_connect(args) -> int:
    servers = _load(args)
    code = _country(args.country, servers)
    state = State()
    ranked = _candidates(code, servers, state)
    if args.home_only:
        ranked = [s for s in ranked if s.residential]
    ranked = ranked[: args.max_tries]
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

    for i, server in enumerate(ranked, 1):
        print(f"[{i}/{len(ranked)}] {server.host} {server.ip} ({server.label}) ... ", end="", flush=True)
        tunnel = Tunnel(ovpn.for_desktop(server), binary, prefix, verbose=args.verbose)
        try:
            try:
                tunnel.start(timeout=args.timeout)
            except TunnelError as e:
                print(f"no connection: {e}")
                state.mark_bad(server)
                state.save()
                continue

            ok, reason = _verify(server, code, args)
            if not ok:
                print(reason)
                tunnel.stop()
                state.mark_bad(server)
                state.save()
                continue

            print(reason)
            state.mark_good(server)
            state.save()
            if _hold(tunnel, server):
                return 0
            # User asked for the next server.
            state.mark_bad(server)
            state.save()
        except KeyboardInterrupt:
            print("\nDisconnecting...")
            return 130
        finally:
            tunnel.stop()

    print("\nNo working server found. VPN Gate's list changes constantly; try again later,")
    print("or try a neighbouring country (`duck countries`).")
    return 1


def _verify(server: Server, code: str, args) -> tuple[bool, str]:
    info = probe.exit_info()
    if not info:
        return False, "connected, but no internet through it"
    if info.get("countryCode") != code:
        return False, f"exits in {info.get('countryCode')}, not {code}"
    if args.test:
        ok, reason = probe.check_site(args.test, expect=args.expect, reject=args.reject or ())
        if not ok:
            return False, f"site blocked: {reason}"
    flagged = " (exit IP flagged as proxy/datacentre)" if info.get("proxy") or info.get("hosting") else ""
    return True, f"OK, exit IP {info.get('query')}{flagged}"


def _hold(tunnel: Tunnel, server: Server) -> bool:
    """Keep the tunnel up until the user is done. Returns False if they want the next server."""
    dropped = threading.Event()

    def on_drop():
        dropped.set()
        print("\n! The tunnel dropped. Press Enter to try the next server.")

    tunnel.on_drop = on_drop
    print(
        f"\nConnected through {server.country}. Check your site in the browser now.\n"
        "  Enter      disconnect and quit\n"
        "  n + Enter  this one doesn't work, try the next server"
    )
    try:
        answer = input("> ").strip().lower()
    except EOFError:
        answer = ""
    if dropped.is_set():
        return False
    return answer not in ("n", "next")


def cmd_export(args) -> int:
    state = State()
    if args.last:
        if not state.last:
            sys.exit("error: no server has worked yet. Run `duck connect` first.")
        chosen = [Server.from_dict(state.last)]
    else:
        if not args.country:
            sys.exit("error: give a country, or use --last")
        servers = _load(args)
        code = _country(args.country, servers)
        chosen = _candidates(code, servers, state)[: args.count]

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


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="duck",
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
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)
