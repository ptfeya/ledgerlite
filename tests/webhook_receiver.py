"""Real loopback HTTP receiver for webhook delivery tests.

Binds only the mission's receiver address. Every request (any method) is
captured with its exact body bytes; a per-test responder decides the reply.
"""

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

RECEIVER_HOST = "127.0.0.1"
RECEIVER_PORT = 8101


@dataclass(frozen=True)
class Captured:
    method: str
    path: str
    headers: dict[str, str]
    body: bytes
    received_at: float


@dataclass
class Reply:
    status: int = 200
    headers: dict[str, str] = field(default_factory=dict)
    body: bytes = b""


# A responder returns a Reply, or a callable that writes the raw response itself
# (for stalls, trickles and dropped connections).
Responder = Callable[[Captured], "Reply | Callable[[BaseHTTPRequestHandler], None]"]


class Receiver:
    def __init__(self) -> None:
        self.requests: list[Captured] = []
        self.responder: Responder = lambda request: Reply(200)
        # Set at teardown so blocked responders always return.
        self.released = threading.Event()
        self._changed = threading.Condition()
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    def url(self, path: str = "/hooks") -> str:
        return f"http://{RECEIVER_HOST}:{RECEIVER_PORT}{path}"

    def paths(self) -> list[str]:
        return [request.path for request in self.requests]

    def wait_for(self, count: int, timeout: float = 15.0) -> list[Captured]:
        deadline = time.monotonic() + timeout
        with self._changed:
            while len(self.requests) < count:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise AssertionError(
                        f"receiver got {len(self.requests)} requests, expected {count}"
                    )
                self._changed.wait(remaining)
            return list(self.requests)

    def _record(self, request: Captured) -> None:
        with self._changed:
            self.requests.append(request)
            self._changed.notify_all()

    def start(self) -> None:
        receiver = self

        class Handler(BaseHTTPRequestHandler):
            def _handle(self) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length else b""
                request = Captured(
                    method=self.command,
                    path=self.path,
                    headers={name.lower(): value for name, value in self.headers.items()},
                    body=body,
                    received_at=time.monotonic(),
                )
                receiver._record(request)
                reply = receiver.responder(request)
                if callable(reply):
                    reply(self)
                    return
                self.send_response(reply.status)
                for name, value in reply.headers.items():
                    self.send_header(name, value)
                self.send_header("Content-Length", str(len(reply.body)))
                self.end_headers()
                self.wfile.write(reply.body)

            do_GET = do_POST = do_PUT = do_HEAD = do_DELETE = _handle

            def log_message(self, format, *args):  # noqa: A002 - stdlib signature
                pass

        server = ThreadingHTTPServer((RECEIVER_HOST, RECEIVER_PORT), Handler)
        server.daemon_threads = True
        self._server = server
        self._thread = threading.Thread(target=server.serve_forever, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self.released.set()
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5)
