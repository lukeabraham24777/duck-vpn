"""macOS menu bar app: toggle the VPN and pick a country from the menu bar."""

from __future__ import annotations

import plistlib
import subprocess
import sys
from pathlib import Path

import rumps

from . import ovpn, paths
from .controller import Controller
from .helper import HelperClient

ICON_OFF = "🦆"
LAUNCH_AGENT = Path.home() / "Library" / "LaunchAgents" / "com.duckvpn.menubar.plist"


def _flag(cc: str) -> str:
    return "".join(chr(0x1F1A5 + ord(c)) for c in cc.upper()) if len(cc) == 2 else ""


def notify(title: str, message: str) -> None:
    # rumps.notification needs an app bundle; osascript works from a plain script.
    def esc(s):
        return s.replace("\\", "\\\\").replace('"', '\\"')

    subprocess.Popen(
        ["osascript", "-e", f'display notification "{esc(message)}" with title "duck-vpn" subtitle "{esc(title)}"'],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


class DuckApp(rumps.App):
    def __init__(self):
        super().__init__("duck-vpn", title=ICON_OFF, quit_button=None)
        self.ctl = Controller(helper_factory=HelperClient.launch, notify=notify)
        self._countries_version = -1
        self._country_items: dict[str, rumps.MenuItem] = {}

        self.status_item = rumps.MenuItem("Off")
        self.toggle_item = rumps.MenuItem("Turn On", callback=self.on_toggle, key="t")
        self.next_item = rumps.MenuItem("Try Another Server")
        self.country_menu = rumps.MenuItem("Country")
        self.country_menu.add(rumps.MenuItem("Loading…"))
        self.home_item = rumps.MenuItem("Home IPs Only", callback=self.on_home_only)
        self.home_item.state = int(self.ctl.prefs["home_only"])
        self.test_item = rumps.MenuItem("Test Site…", callback=self.on_test_site)
        self.export_item = rumps.MenuItem("Send Server to iPhone…", callback=self.on_export)
        self.menu = [
            self.status_item,
            None,
            self.toggle_item,
            self.next_item,
            None,
            self.country_menu,
            self.home_item,
            self.test_item,
            rumps.MenuItem("Refresh Server List", callback=lambda _: self.ctl.refresh_countries(force=True)),
            None,
            self.export_item,
            rumps.MenuItem("Quit duck-vpn", callback=self.on_quit, key="q"),
        ]
        self.ctl.refresh_countries()
        self._timer = rumps.Timer(self.refresh, 0.5)
        self._timer.start()

    # -- periodic UI refresh (main thread) -----------------------------------------

    def refresh(self, _=None) -> None:
        snap = self.ctl.snapshot()
        self.status_item.title = snap.status if not snap.exit_ip else f"{snap.status} · {snap.exit_ip}"
        if snap.phase == "on" and snap.server:
            self.title = f"{ICON_OFF} {snap.server.country_code}"
        elif snap.phase in ("connecting", "stopping"):
            self.title = f"{ICON_OFF}…"
        else:
            self.title = ICON_OFF
        self.toggle_item.title = "Turn On" if snap.phase == "off" else "Turn Off"
        self.next_item.set_callback(self.on_next if snap.phase == "on" else None)
        if snap.countries_version != self._countries_version:
            self._countries_version = snap.countries_version
            self._rebuild_countries(snap.countries)
        for code, item in self._country_items.items():
            item.state = int(code == self.ctl.prefs["country"])

    def _rebuild_countries(self, countries) -> None:
        if not countries:
            return
        self.country_menu.clear()
        self._country_items = {}
        selected = self.ctl.prefs["country"]
        known = {c for c, _, _ in countries}
        if selected not in known:
            # Keep the saved choice visible even if it has no servers right now.
            countries = [(selected, selected, 0)] + list(countries)
        for code, name, count in sorted(countries, key=lambda t: t[1]):
            item = rumps.MenuItem(f"{_flag(code)} {name} ({count})", callback=self.on_country)
            self._country_items[code] = item
            self.country_menu.add(item)

    # -- menu actions ----------------------------------------------------------------

    def on_toggle(self, _):
        if self.ctl.snapshot().phase == "off":
            self.ctl.turn_on()
        else:
            self.ctl.turn_off()

    def on_next(self, _):
        self.ctl.next_server()

    def on_country(self, item):
        code = next(c for c, i in self._country_items.items() if i is item)
        self.ctl.set_country(code)

    def on_home_only(self, item):
        item.state = int(not item.state)
        self.ctl.set_pref("home_only", bool(item.state))

    def on_test_site(self, _):
        _activate()
        resp = rumps.Window(
            message=(
                "Optional: a page that must load through the VPN, e.g. the show you want to watch.\n"
                "Leave empty to only check the country."
            ),
            title="Test site",
            default_text=self.ctl.prefs["test_url"],
            ok="Save",
            cancel="Cancel",
            dimensions=(340, 24),
        ).run()
        if resp.clicked:
            self.ctl.set_pref("test_url", resp.text.strip())

    def on_export(self, _):
        server = self.ctl.last_server()
        if not server:
            rumps.alert("Nothing to send yet", "Connect to a working server first.")
            return
        path = Path.home() / "Downloads" / ovpn.filename(server)
        path.write_text(ovpn.for_mobile(server), encoding="utf-8")
        subprocess.run(["open", "-R", str(path)])
        notify(
            "Server saved to Downloads",
            "AirDrop it to your iPhone and open it in OpenVPN Connect.",
        )

    def on_quit(self, _):
        self.title = f"{ICON_OFF}…"
        self.ctl.shutdown()
        rumps.quit_application()


def _activate() -> None:
    try:
        from AppKit import NSApplication

        NSApplication.sharedApplication().activateIgnoringOtherApps_(True)
    except ImportError:
        pass


def run() -> int:
    try:
        from AppKit import NSApplication

        # Accessory = menu bar only, no Dock icon.
        NSApplication.sharedApplication().setActivationPolicy_(1)
    except ImportError:
        pass
    DuckApp().run()
    return 0


def launch_background() -> Path:
    log = paths.data_dir() / "menubar.log"
    with open(log, "a") as out:
        subprocess.Popen(
            [sys.executable, "-m", "duckvpn", "menubar", "--foreground"],
            stdout=out,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
    return log


def set_autostart(enabled: bool) -> Path:
    if not enabled:
        LAUNCH_AGENT.unlink(missing_ok=True)
        return LAUNCH_AGENT
    LAUNCH_AGENT.parent.mkdir(parents=True, exist_ok=True)
    log = str(paths.data_dir() / "menubar.log")
    plist = {
        "Label": "com.duckvpn.menubar",
        "ProgramArguments": [sys.executable, "-m", "duckvpn", "menubar", "--foreground"],
        "RunAtLoad": True,
        "StandardOutPath": log,
        "StandardErrorPath": log,
    }
    with open(LAUNCH_AGENT, "wb") as f:
        plistlib.dump(plist, f)
    return LAUNCH_AGENT
