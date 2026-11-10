"""Stand-in for an SSH tunnel when testing on one computer: plain TCP forwarding, like ``ssh -L``.

    python -m qumus_lab.tunnel_sim 18781:127.0.0.1:8781 18782:127.0.0.1:8782

The brain then uses http://127.0.0.1:18781 / 18782 exactly as it would use the tunnel ends of an
``ssh -R`` from the lab PC (framework plan: brain-side tunnel ends 127.0.0.1:18781 / 18782). The module
ports see every forwarded request coming from 127.0.0.1, which is why they never trust loopback (PR #7).
"""

from __future__ import annotations

import socket
import sys
import threading


def _pipe(src: socket.socket, dst: socket.socket) -> None:
    try:
        while True:
            data = src.recv(65536)
            if not data:
                break
            dst.sendall(data)
    except OSError:
        pass
    finally:
        for s in (src, dst):
            try:
                s.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            s.close()


class Forwarder:
    def __init__(self, listen_port: int, target_host: str, target_port: int, listen_host: str = "127.0.0.1"):
        self.target = (target_host, int(target_port))
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind((listen_host, int(listen_port)))
        self.sock.listen(32)
        self.port = self.sock.getsockname()[1]
        self.connections = 0
        self._thread = threading.Thread(target=self._accept, daemon=True, name=f"tunnel-{self.port}")

    def start(self) -> Forwarder:
        self._thread.start()
        return self

    def _accept(self) -> None:
        while True:
            try:
                client, _ = self.sock.accept()
            except OSError:
                return
            try:
                upstream = socket.create_connection(self.target, timeout=10)
                upstream.settimeout(None)
            except OSError:
                client.close()
                continue
            self.connections += 1
            threading.Thread(target=_pipe, args=(client, upstream), daemon=True).start()
            threading.Thread(target=_pipe, args=(upstream, client), daemon=True).start()

    def close(self) -> None:
        self.sock.close()


def main(argv: list[str] | None = None) -> int:
    specs = argv if argv is not None else sys.argv[1:]
    if not specs:
        print(__doc__)
        return 2
    fwd = []
    for spec in specs:
        lp, host, tp = spec.split(":")
        fwd.append(Forwarder(int(lp), host, int(tp)).start())
        print(f"127.0.0.1:{lp} -> {host}:{tp}", flush=True)
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        pass
    for f in fwd:
        f.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
