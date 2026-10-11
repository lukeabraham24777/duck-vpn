"""UI-independent logic behind the menu bar app.

All slow work (downloading the list, connecting, testing) runs on worker
threads. The UI reads `snapshot()` on a timer and calls the public methods.
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field
from typing import Callable, Optional

from . import paths, session, vpngate
from .openvpn import TunnelError
from .state import State
from .vpngate import FetchError, Server

DEFAULT_PREFS = {"country": "JP", "test_url": "", "home_only": False, "max_tries": 15}


def load_prefs() -> dict:
    try:
        saved = json.loads((paths.data_dir() / "menubar.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        saved = {}
    return {**DEFAULT_PREFS, **{k: v for k, v in saved.items() if k in DEFAULT_PREFS}}


def save_prefs(prefs: dict) -> None:
    (paths.data_dir() / "menubar.json").write_text(json.dumps(prefs, indent=1), encoding="utf-8")


@dataclass
class Snapshot:
    phase: str = "off"  # off | connecting | on | stopping
    status: str = "Off"
    server: Optional[Server] = None
    exit_ip: str = ""
    countries: list = field(default_factory=list)  # (code, name, count)
    countries_version: int = 0


class Controller:
    def __init__(self, helper_factory: Callable[[], object], notify: Callable[[str, str], None]):
        """helper_factory() returns an object with .tunnel(config) and .close()."""
        self._helper_factory = helper_factory
        self._helper = None
        self.notify = notify
        self.prefs = load_prefs()
        self._snap = Snapshot()
        self._lock = threading.Lock()
        self._servers: list[Server] = []
        self._worker: Optional[threading.Thread] = None
        self._connector: Optional[session.Connector] = None
        self._command = threading.Event()
        self._command_value = ""

    # -- state for the UI --------------------------------------------------------

    def snapshot(self) -> Snapshot:
        with self._lock:
            return Snapshot(**vars(self._snap))

    def _set(self, **changes) -> None:
        with self._lock:
            for k, v in changes.items():
                setattr(self._snap, k, v)

    # -- server list -------------------------------------------------------------

    def refresh_countries(self, force: bool = False) -> None:
        def work():
            try:
                servers = vpngate.fetch(max_age=0 if force else vpngate.CACHE_TTL)
            except FetchError as e:
                if self.snapshot().phase == "off":
                    self._set(status="Couldn't load servers")
                self.notify("Couldn't load the server list", str(e))
                return
            self._servers = servers
            snap = self.snapshot()
            self._set(countries=vpngate.countries(servers), countries_version=snap.countries_version + 1)

        threading.Thread(target=work, daemon=True).start()

    # -- preferences -------------------------------------------------------------

    def set_pref(self, key: str, value) -> None:
        self.prefs[key] = value
        save_prefs(self.prefs)

    def set_country(self, code: str) -> None:
        changed = code != self.prefs["country"]
        self.set_pref("country", code)
        if changed and self.snapshot().phase in ("on", "connecting"):
            self.turn_off(then_on=True)

    # -- connecting --------------------------------------------------------------

    def turn_on(self) -> None:
        if self._worker and self._worker.is_alive():
            return
        self._command.clear()
        self._set(phase="connecting", status="Starting…", server=None, exit_ip="")
        self._worker = threading.Thread(target=self._run, daemon=True)
        self._worker.start()

    def turn_off(self, then_on: bool = False) -> None:
        if self._connector:
            self._connector.cancelled.set()
        self._command_value = "restart" if then_on else "off"
        self._command.set()
        if self.snapshot().phase != "off":
            self._set(phase="stopping", status="Disconnecting…")

    def next_server(self) -> None:
        self._command_value = "next"
        self._command.set()

    def shutdown(self, wait: float = 20) -> None:
        self.turn_off()
        if self._worker:
            self._worker.join(wait)
        if self._helper:
            self._helper.close()
            self._helper = None

    def _ensure_helper(self):
        if self._helper is None:
            self._helper = self._helper_factory()
        return self._helper

    def _run(self) -> None:
        restart = False
        try:
            restart = self._connect_once()
        except Exception as e:  # never let the worker die silently
            self.notify("duck-vpn error", str(e))
        finally:
            self._connector = None
            if self.snapshot().phase != "off":
                self._set(phase="off", status="Off", server=None, exit_ip="")
        if restart:
            self._worker = None  # this thread is finishing; let turn_on start a new one
            self.turn_on()

    def _connect_once(self) -> bool:
        """Returns True if the VPN should immediately reconnect (country changed)."""
        try:
            helper = self._ensure_helper()
        except TunnelError as e:
            self.notify("VPN not started", str(e))
            return False

        if not self._servers:
            self._set(status="Loading servers…")
            try:
                self._servers = vpngate.fetch()
            except FetchError as e:
                self.notify("Couldn't load the server list", str(e))
                return False

        code = self.prefs["country"]
        state = State()
        ranked = session.candidates(code, self._servers, state, home_only=self.prefs["home_only"])
        ranked = ranked[: int(self.prefs["max_tries"])]
        if not ranked:
            self.notify("No servers", f"No {'home-IP ' if self.prefs['home_only'] else ''}servers in {code} right now.")
            return False
        country = ranked[0].country

        def on_event(kind, server=None, index=0, total=0, reason="", info=None):
            if kind == "trying":
                self._set(status=f"Trying {country} server {index}/{total}…")
            elif kind == "connected":
                self._set(phase="on", status=f"Connected: {country}", server=server, exit_ip=info.get("query", ""))
                self.notify(f"Connected through {country}", f"Exit IP {info.get('query')} ({server.label})")
            elif kind == "exhausted":
                self.notify("No working server found", f"Tried {len(ranked)} servers in {country}. Try again later.")

        def make_tunnel(config):
            nonlocal helper
            try:
                helper.request(cmd="status")
            except TunnelError:
                # The helper went away (e.g. killed during sleep); start a new one.
                self._helper = None
                helper = self._ensure_helper()
            return helper.tunnel(config)

        self._connector = session.Connector(
            code,
            ranked,
            make_tunnel=make_tunnel,
            on_event=on_event,
            state=state,
            test=self.prefs["test_url"] or None,
        )
        self._connector.run(self._hold)
        return self._command_value == "restart"

    def _hold(self, server: Server, tunnel, info: dict) -> str:
        # The command flag is cleared in turn_on, so an "off" that arrived
        # while we were still testing this server is honoured here.
        while True:
            if self._command.wait(3):
                self._command.clear()
                if self._command_value == "next":
                    self._set(phase="connecting", status="Trying another server…")
                    return "next"
                return "done"
            if not tunnel.alive:
                self.notify("Connection dropped", "Trying another server…")
                self._set(phase="connecting", status="Reconnecting…")
                return "next"

    def last_server(self) -> Optional[Server]:
        snap = self.snapshot()
        if snap.server:
            return snap.server
        last = State().last
        return Server.from_dict(last) if last else None
