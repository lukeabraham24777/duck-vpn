"""The find-a-working-server loop, shared by the CLI and the menu bar app."""

from __future__ import annotations

import threading
from typing import Callable, Optional, Protocol

from . import ovpn, probe, reputation, vpngate
from .openvpn import TunnelError
from .state import State
from .vpngate import Server


class TunnelLike(Protocol):
    alive: bool

    def start(self, timeout: float = ...) -> None: ...
    def stop(self) -> None: ...


# hold(server, tunnel, exit_info) -> "done" to finish, "next" to try another server.
HoldFn = Callable[[Server, TunnelLike, dict], str]


def candidates(code: str, servers: list[Server], state: State, home_only: bool = False) -> list[Server]:
    fresh = [s for s in servers if s.country_code == code]
    # Servers that worked before but dropped off the current list may still be up.
    fresh_ips = {s.ip for s in fresh}
    remembered = state.good_servers(code)
    pool = fresh + [s for s in remembered if s.ip not in fresh_ips]
    reputation.annotate(pool)
    ranked = vpngate.rank(pool, known_good=[s.ip for s in remembered], known_bad=state.bad_ips())
    return [s for s in ranked if s.residential] if home_only else ranked


def verify(code: str, test: Optional[str], expect: Optional[str], reject) -> tuple[bool, str, dict]:
    info = probe.exit_info()
    if not info:
        return False, "connected, but no internet through it", {}
    if info.get("countryCode") != code:
        return False, f"exits in {info.get('countryCode')}, not {code}", info
    if test:
        ok, reason = probe.check_site(test, expect=expect, reject=reject or ())
        if not ok:
            return False, f"site blocked: {reason}", info
    return True, "OK", info


class Connector:
    """Try ranked servers until one passes the checks, then hand it to `hold`.

    Progress is reported through on_event(kind, **data):
      trying(index, total, server) / failed(server, reason) /
      connected(server, info) / exhausted()
    """

    def __init__(
        self,
        code: str,
        ranked: list[Server],
        make_tunnel: Callable[[str], TunnelLike],
        on_event: Callable[..., None],
        state: Optional[State] = None,
        test: Optional[str] = None,
        expect: Optional[str] = None,
        reject=(),
        timeout: float = 25,
    ):
        self.code = code
        self.ranked = ranked
        self.make_tunnel = make_tunnel
        self.on_event = on_event
        self.state = state or State()
        self.test, self.expect, self.reject = test, expect, reject
        self.timeout = timeout
        self.cancelled = threading.Event()

    def _fail(self, server: Server, reason: str) -> None:
        self.on_event("failed", server=server, reason=reason)
        self.state.mark_bad(server)
        self.state.save()

    def run(self, hold: HoldFn) -> bool:
        """Returns True if the user finished while connected to a working server."""
        for i, server in enumerate(self.ranked, 1):
            if self.cancelled.is_set():
                return False
            self.on_event("trying", index=i, total=len(self.ranked), server=server)
            try:
                tunnel = self.make_tunnel(ovpn.for_desktop(server))
            except ValueError as e:  # config rejected by the sanitizer
                self._fail(server, str(e))
                continue
            try:
                try:
                    tunnel.start(timeout=self.timeout)
                except TunnelError as e:
                    self._fail(server, f"no connection: {e}")
                    continue
                if self.cancelled.is_set():
                    return False
                ok, reason, info = verify(self.code, self.test, self.expect, self.reject)
                if not ok:
                    tunnel.stop()
                    self._fail(server, reason)
                    continue
                self.state.mark_good(server)
                self.state.save()
                self.on_event("connected", server=server, info=info)
                if hold(server, tunnel, info) == "done":
                    return True
                self._fail(server, "skipped")
            finally:
                tunnel.stop()
        if not self.cancelled.is_set():
            self.on_event("exhausted")
        return False
