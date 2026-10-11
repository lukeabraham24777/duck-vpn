"""Start and stop the OpenVPN command-line client on Windows and macOS."""

from __future__ import annotations

import collections
import os
import re
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Callable, Optional

CANDIDATE_PATHS = [
    r"C:\Program Files\OpenVPN\bin\openvpn.exe",
    r"C:\Program Files (x86)\OpenVPN\bin\openvpn.exe",
    "/opt/homebrew/sbin/openvpn",
    "/usr/local/sbin/openvpn",
    "/opt/local/sbin/openvpn",
    "/usr/sbin/openvpn",
]

READY_MARKER = "Initialization Sequence Completed"
FATAL_MARKERS = ("Exiting due to fatal error", "AUTH_FAILED", "Options error", "Cannot load")


class TunnelError(Exception):
    pass


def find_openvpn() -> Optional[str]:
    env = os.environ.get("DUCKVPN_OPENVPN")
    if env:
        return env
    found = shutil.which("openvpn")
    if found:
        return found
    return next((p for p in CANDIDATE_PATHS if Path(p).exists()), None)


def privilege_prefix() -> list[str]:
    """Return the command prefix needed to run OpenVPN with admin rights.

    OpenVPN must create a network adapter and change routes, which needs
    Administrator on Windows and root on macOS/Linux. On Unix we ask for the
    sudo password once up front, then reuse it non-interactively.
    """
    if sys.platform == "win32":
        import ctypes

        if not ctypes.windll.shell32.IsUserAnAdmin():
            raise TunnelError(
                "OpenVPN needs Administrator rights. Right-click your terminal, "
                "choose 'Run as administrator', and run the command again."
            )
        return []
    if os.geteuid() == 0:
        return []
    print("OpenVPN needs admin rights to change your network routes.")
    if subprocess.call(["sudo", "-v"]) != 0:
        raise TunnelError("sudo authentication failed")
    return ["sudo", "-n"]


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Tunnel:
    def __init__(self, config: str, binary: str, prefix: list[str], verbose: bool = False):
        self.config = config
        self.binary = binary
        self.prefix = prefix
        self.verbose = verbose
        self.log: collections.deque[str] = collections.deque(maxlen=40)
        self.on_drop: Optional[Callable[[], None]] = None
        self._ready = threading.Event()
        self._failed = threading.Event()
        self._stopping = False
        self._proc: Optional[subprocess.Popen] = None
        self._dir: Optional[str] = None

    def start(self, timeout: float = 30) -> None:
        self._dir = tempfile.mkdtemp(prefix="duckvpn-")
        cfg = Path(self._dir) / "server.ovpn"
        cfg.write_text(self.config, encoding="utf-8")
        # Password-protect the management port so other local programs can't
        # control the tunnel.
        self._password = secrets.token_hex(16)
        pw = Path(self._dir) / "mgmt.txt"
        pw.write_text(self._password + "\n", encoding="utf-8")
        os.chmod(pw, 0o600)
        self._port = _free_port()
        cmd = self.prefix + [
            self.binary,
            "--config", str(cfg),
            # Comes after --config so it overrides anything in the file: allow
            # OpenVPN's built-in route/ifconfig calls, never external scripts.
            "--script-security", "1",
            "--management", "127.0.0.1", str(self._port), str(pw),
            "--connect-retry-max", "1",
            "--connect-timeout", "10",
            "--resolv-retry", "0",
            "--verb", "3",
        ]
        self._proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )
        threading.Thread(target=self._read_output, daemon=True).start()

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self._ready.wait(0.25):
                return
            if self._failed.is_set() or self._proc.poll() is not None:
                break
        reason = self._failure_reason()
        self.stop()
        raise TunnelError(reason)

    def _read_output(self) -> None:
        assert self._proc and self._proc.stdout
        for line in self._proc.stdout:
            line = line.rstrip()
            self.log.append(line)
            if self.verbose:
                print("    openvpn:", line)
            if READY_MARKER in line:
                self._ready.set()
            elif any(m in line for m in FATAL_MARKERS):
                self._failed.set()
        if self._ready.is_set() and not self._stopping and self.on_drop:
            self.on_drop()

    def _failure_reason(self) -> str:
        for line in reversed(self.log):
            if any(m in line for m in FATAL_MARKERS) or "error" in line.lower():
                # Drop OpenVPN's leading "YYYY-MM-DD HH:MM:SS " timestamp.
                return re.sub(r"^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d ", "", line)[:160]
        return "timed out connecting" if self._proc and self._proc.poll() is None else "connection failed"

    @property
    def alive(self) -> bool:
        return bool(self._proc and self._proc.poll() is None and self._ready.is_set())

    def _send_sigterm(self) -> None:
        """Ask OpenVPN to shut down cleanly so it removes its routes."""
        try:
            with socket.create_connection(("127.0.0.1", self._port), timeout=3) as s:
                s.settimeout(3)
                buf = b""
                while b"PASSWORD" not in buf:
                    chunk = s.recv(256)
                    if not chunk:
                        return
                    buf += chunk
                s.sendall(self._password.encode() + b"\nsignal SIGTERM\n")
                time.sleep(0.3)
        except OSError:
            pass

    def stop(self) -> None:
        self._stopping = True
        proc = self._proc
        if proc and proc.poll() is None:
            self._send_sigterm()
            try:
                proc.wait(10)
            except subprocess.TimeoutExpired:
                proc.terminate()
                try:
                    proc.wait(5)
                except subprocess.TimeoutExpired:
                    proc.kill()
        if self._dir:
            shutil.rmtree(self._dir, ignore_errors=True)
            self._dir = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.stop()
