"""Privileged tunnel runner for the menu bar app (macOS).

A menu bar app can't answer a sudo prompt, so when you first turn the VPN on
it asks for your password once (the standard macOS dialog) and starts this
helper as root. The helper does one job: start and stop OpenVPN with configs
it re-sanitizes itself. Everything else (fetching, ranking, testing) stays in
the unprivileged app.

The helper listens on a Unix socket that only your user can open, serves one
connection, and exits (taking the tunnel down) as soon as the app goes away.

Protocol: one JSON object per line.
  {"cmd": "start", "config": "...", "timeout": 25} -> {"ok": true} | {"ok": false, "error": "..."}
  {"cmd": "stop"}   -> {"ok": true}
  {"cmd": "status"} -> {"ok": true, "alive": bool}
"""

from __future__ import annotations

import json
import os
import shlex
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Optional

from . import ovpn, paths
from .openvpn import Tunnel, TunnelError, find_openvpn

ACCEPT_TIMEOUT = 60


def socket_path() -> Path:
    return paths.data_dir() / "helper.sock"


# --- root side ----------------------------------------------------------------


def serve(sock_path: str, uid: int, binary: Optional[str] = None) -> int:
    binary = binary or find_openvpn()
    if not binary:
        return 1
    try:
        os.unlink(sock_path)
    except FileNotFoundError:
        pass
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    old_umask = os.umask(0o177)
    try:
        srv.bind(sock_path)
    finally:
        os.umask(old_umask)
    if os.geteuid() == 0:
        os.chown(sock_path, uid, -1, follow_symlinks=False)
    srv.listen(1)
    srv.settimeout(ACCEPT_TIMEOUT)
    tunnel: Optional[Tunnel] = None
    try:
        conn, _ = srv.accept()
        conn.settimeout(None)
        with conn, conn.makefile("rw", encoding="utf-8", newline="\n") as f:
            for line in f:
                try:
                    msg = json.loads(line)
                except ValueError:
                    break
                cmd = msg.get("cmd")
                if cmd == "start":
                    if tunnel:
                        tunnel.stop()
                    try:
                        tunnel = Tunnel(ovpn.sanitize(str(msg.get("config", ""))), binary, prefix=[])
                        tunnel.start(timeout=min(float(msg.get("timeout", 25)), 60))
                        reply = {"ok": True}
                    except (TunnelError, ValueError) as e:
                        tunnel = None
                        reply = {"ok": False, "error": str(e)}
                elif cmd == "stop":
                    if tunnel:
                        tunnel.stop()
                        tunnel = None
                    reply = {"ok": True}
                elif cmd == "status":
                    reply = {"ok": True, "alive": bool(tunnel and tunnel.alive)}
                else:
                    break
                f.write(json.dumps(reply) + "\n")
                f.flush()
    except (socket.timeout, OSError):
        pass
    finally:
        if tunnel:
            tunnel.stop()
        srv.close()
        try:
            os.unlink(sock_path)
        except OSError:
            pass
    return 0


# --- app side -----------------------------------------------------------------


class HelperClient:
    def __init__(self, sock: socket.socket):
        self._sock = sock
        self._file = sock.makefile("rw", encoding="utf-8", newline="\n")
        self._lock = threading.Lock()

    @classmethod
    def connect(cls, path: Path, wait: float = 15) -> "HelperClient":
        deadline = time.monotonic() + wait
        while True:
            try:
                s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                s.connect(str(path))
                return cls(s)
            except OSError:
                s.close()
                if time.monotonic() > deadline:
                    raise TunnelError("the VPN helper didn't start")
                time.sleep(0.2)

    @classmethod
    def launch(cls) -> "HelperClient":
        """Start the helper as root via the macOS password dialog."""
        path = socket_path()
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        args = [sys.executable, "-m", "duckvpn.helper", str(path), str(os.getuid())]
        openvpn = find_openvpn()
        if openvpn:
            args.append(openvpn)
        shell = " ".join(shlex.quote(a) for a in args) + " >/dev/null 2>&1 &"
        escaped = shell.replace("\\", "\\\\").replace('"', '\\"')
        script = (
            f'do shell script "{escaped}" with administrator privileges '
            'with prompt "duck-vpn needs your password to route your traffic through the VPN."'
        )
        r = subprocess.run(["osascript", "-e", script], capture_output=True, text=True)
        if r.returncode != 0:
            if "-128" in r.stderr:
                raise TunnelError("password prompt cancelled")
            raise TunnelError(r.stderr.strip() or "couldn't start the VPN helper")
        return cls.connect(path)

    def request(self, **msg) -> dict:
        with self._lock:
            try:
                self._file.write(json.dumps(msg) + "\n")
                self._file.flush()
                line = self._file.readline()
            except OSError as e:
                raise TunnelError(f"lost contact with the VPN helper: {e}") from e
        if not line:
            raise TunnelError("the VPN helper exited")
        return json.loads(line)

    def tunnel(self, config: str) -> "RemoteTunnel":
        return RemoteTunnel(self, config)

    def close(self) -> None:
        try:
            self._file.close()
            self._sock.close()
        except OSError:
            pass


class RemoteTunnel:
    """Same interface as openvpn.Tunnel, but the helper does the work."""

    def __init__(self, client: HelperClient, config: str):
        self.client = client
        self.config = config
        self.on_drop = None

    def start(self, timeout: float = 25) -> None:
        reply = self.client.request(cmd="start", config=self.config, timeout=timeout)
        if not reply.get("ok"):
            raise TunnelError(reply.get("error", "connection failed"))

    def stop(self) -> None:
        try:
            self.client.request(cmd="stop")
        except TunnelError:
            pass

    @property
    def alive(self) -> bool:
        try:
            return bool(self.client.request(cmd="status").get("alive"))
        except TunnelError:
            return False


if __name__ == "__main__":
    if len(sys.argv) not in (3, 4):
        sys.exit("usage: python -m duckvpn.helper SOCKET UID [OPENVPN]")
    sys.exit(serve(sys.argv[1], int(sys.argv[2]), sys.argv[3] if len(sys.argv) == 4 else None))
