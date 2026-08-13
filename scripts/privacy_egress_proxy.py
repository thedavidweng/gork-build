#!/usr/bin/env python3
"""Minimal HTTP CONNECT proxy that records destination hosts.

Used by Gork Build privacy CI: run the release binary through this proxy and
assert no denylisted destinations appear. HTTPS traffic is not decrypted —
only CONNECT hostnames are logged (sufficient for egress inventory checks).

Usage:
  python3 scripts/privacy_egress_proxy.py --listen 127.0.0.1:18080 --log /tmp/hosts.txt
"""

from __future__ import annotations

import argparse
import select
import socket
import socketserver
import sys
import threading
from typing import Optional


class HostLog:
    def __init__(self, path: str) -> None:
        self._path = path
        self._lock = threading.Lock()
        open(path, "w", encoding="utf-8").close()

    def add(self, host: str) -> None:
        host = host.strip().lower()
        if not host:
            return
        with self._lock:
            with open(self._path, "a", encoding="utf-8") as f:
                f.write(host + "\n")


class ProxyHandler(socketserver.BaseRequestHandler):
    log: HostLog  # set on server class

    def handle(self) -> None:
        self.request.settimeout(30.0)
        try:
            data = self._recv_headers()
        except OSError:
            return
        if not data:
            return
        first = data.split(b"\r\n", 1)[0].decode("latin-1", errors="replace")
        parts = first.split()
        if len(parts) < 2:
            return
        method, target = parts[0].upper(), parts[1]
        if method == "CONNECT":
            hostport = target
            host = hostport.split(":")[0]
            self.log.add(host)
            try:
                remote = self._connect(hostport)
            except OSError:
                self.request.sendall(b"HTTP/1.1 502 Bad Gateway\r\n\r\n")
                return
            self.request.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            self._pipe(self.request, remote)
            return

        # Plain HTTP: parse Host header for logging
        host = self._host_from_headers(data) or "unknown"
        self.log.add(host.split(":")[0])
        # Best-effort forward for HTTP (rarely used by the CLI).
        try:
            remote = self._connect(host if ":" in host else f"{host}:80")
            remote.sendall(data)
            self._pipe(self.request, remote)
        except OSError:
            pass

    def _recv_headers(self) -> bytes:
        buf = b""
        while b"\r\n\r\n" not in buf and len(buf) < 65536:
            chunk = self.request.recv(4096)
            if not chunk:
                break
            buf += chunk
        return buf

    @staticmethod
    def _host_from_headers(data: bytes) -> Optional[str]:
        for line in data.split(b"\r\n"):
            if line.lower().startswith(b"host:"):
                return line.split(b":", 1)[1].strip().decode("latin-1", errors="replace")
        return None

    @staticmethod
    def _connect(hostport: str) -> socket.socket:
        if ":" in hostport:
            host, port_s = hostport.rsplit(":", 1)
            port = int(port_s)
        else:
            host, port = hostport, 443
        remote = socket.create_connection((host, port), timeout=15)
        remote.settimeout(30.0)
        return remote

    @staticmethod
    def _pipe(a: socket.socket, b: socket.socket) -> None:
        sockets = [a, b]
        try:
            while True:
                r, _, _ = select.select(sockets, [], [], 30.0)
                if not r:
                    break
                for s in r:
                    other = b if s is a else a
                    try:
                        data = s.recv(8192)
                    except OSError:
                        return
                    if not data:
                        return
                    try:
                        other.sendall(data)
                    except OSError:
                        return
        finally:
            for s in (a, b):
                try:
                    s.close()
                except OSError:
                    pass


class ThreadingTCPServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--listen", default="127.0.0.1:18080")
    ap.add_argument("--log", required=True, help="Append-only host log path")
    args = ap.parse_args()
    host, port_s = args.listen.rsplit(":", 1)
    port = int(port_s)
    ProxyHandler.log = HostLog(args.log)
    with ThreadingTCPServer((host, port), ProxyHandler) as srv:
        print(f"privacy_egress_proxy listening on {host}:{port}, log={args.log}", flush=True)
        try:
            srv.serve_forever()
        except KeyboardInterrupt:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
