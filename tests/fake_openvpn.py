#!/usr/bin/env python3
"""Stand-in for the openvpn binary used by test_tunnel.

Mimics the parts duck-vpn relies on: prints the ready marker (or a fatal
error when the config contains FAIL), and exits when told
"signal SIGTERM" on the password-protected management port.
"""

import socket
import sys

args = sys.argv[1:]
config = open(args[args.index("--config") + 1]).read()
mgmt = args.index("--management")
port = int(args[mgmt + 2])
password = open(args[mgmt + 3]).read().strip()

if "FAIL" in config:
    print("2024-01-01 00:00:00 Options error: bad things", flush=True)
    print("2024-01-01 00:00:00 Exiting due to fatal error", flush=True)
    sys.exit(1)

srv = socket.socket()
srv.bind(("127.0.0.1", port))
srv.listen(1)
print("2024-01-01 00:00:00 Initialization Sequence Completed", flush=True)

while True:
    conn, _ = srv.accept()
    conn.sendall(b"ENTER PASSWORD:")
    data = b""
    while b"SIGTERM" not in data:
        chunk = conn.recv(256)
        if not chunk:
            break
        data += chunk
    lines = data.decode().splitlines()
    if lines and lines[0] == password and "signal SIGTERM" in lines:
        print("2024-01-01 00:00:00 SIGTERM received, process exiting", flush=True)
        sys.exit(0)
    conn.close()
