"""Serve the responsive UI and dispatch authenticated requests on the Tk thread."""

import hmac
import ipaddress
import json
import queue
import secrets
import socket
import threading
import traceback
from concurrent.futures import Future, TimeoutError
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit


WEB = Path(__file__).resolve().parent / "web"
ASSETS = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/app.css": ("app.css", "text/css; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/campfire.png": ("campfire.png", "image/png"),
}


class WebError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


class WidgetWeb:
    def __init__(self, root, dispatch, busy, lan=False, port=8765):
        self.root = root
        self.dispatch = dispatch
        self.busy = busy
        self.lan = lan
        self.token = secrets.token_urlsafe(24)
        self.requests = queue.Queue(maxsize=32)
        bridge = self

        class Handler(BaseHTTPRequestHandler):
            def setup(self):
                super().setup()
                self.connection.settimeout(5)

            def log_message(self, *args):
                pass

            def send(self, status, mime, body):
                self.send_response(status)
                self.send_header("Content-Type", mime)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("Referrer-Policy", "no-referrer")
                self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'self'; "
                                 "object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'")
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                self.route("GET")

            def do_POST(self):
                self.route("POST")

            def route(self, method):
                try:
                    source = ipaddress.ip_address(self.client_address[0])
                    if not (source.is_loopback or (bridge.lan and source.is_private)):
                        raise WebError("Dostęp tylko z sieci lokalnej.", 403)
                    path = urlsplit(self.path).path
                    if method == "GET" and path in ASSETS:
                        filename, mime = ASSETS[path]
                        return self.send(200, mime, (WEB / filename).read_bytes())
                    if (method, path) not in {("GET", "/api/state"), ("POST", "/api/action")}:
                        raise WebError("Nie znaleziono strony.", 404)
                    supplied = self.headers.get("Authorization", "").encode("utf-8")
                    if not hmac.compare_digest(supplied, b"Bearer " + bridge.token.encode("ascii")):
                        raise WebError("Wprowadź kod z widgetu Windows.", 401)
                    if method == "POST":
                        if self.headers.get_content_type() != "application/json":
                            raise WebError("Oczekiwano JSON.", 415)
                        try:
                            length = int(self.headers.get("Content-Length", "0"))
                            if not 0 < length <= 8192:
                                raise ValueError
                            action = json.loads(self.rfile.read(length))
                            if not isinstance(action, dict):
                                raise ValueError
                        except ValueError:
                            raise WebError("Nieprawidłowe żądanie.")
                    else:
                        action = None
                    result = bridge.request(action)
                    self.send_json(200, result)
                except WebError as error:
                    self.send_json(error.status, {"error": str(error)})
                except (ConnectionError, TimeoutError):
                    pass
                except Exception:
                    traceback.print_exc()
                    self.send_json(500, {"error": "Błąd widgetu Windows."})

            def send_json(self, status, payload):
                self.send(status, "application/json; charset=utf-8",
                          json.dumps(payload, ensure_ascii=False).encode("utf-8"))

        self.server = ThreadingHTTPServer(("0.0.0.0" if lan else "127.0.0.1", port), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.timer = self.root.after(75, self.drain)

    @property
    def port(self):
        return self.server.server_port

    def link(self, host="127.0.0.1"):
        return f"http://{host}:{self.port}/#key={self.token}"

    @staticmethod
    def local_ip():
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.connect(("192.0.2.1", 9))
            return probe.getsockname()[0]

    def request(self, action):
        future = Future()
        try:
            self.requests.put_nowait((action, future))
            return future.result(timeout=4)
        except queue.Full:
            raise WebError("Widget jest zajęty. Spróbuj ponownie.", 503)
        except TimeoutError:
            if future.cancel():
                raise WebError("Zamknij okno dialogowe widgetu i spróbuj ponownie.", 503)
            return future.result()

    def drain(self):
        for _ in range(4):
            try:
                action, future = self.requests.get_nowait()
            except queue.Empty:
                break
            if future.set_running_or_notify_cancel():
                try:
                    if action is not None and self.busy():
                        raise WebError("Zamknij okno dialogowe widgetu i spróbuj ponownie.", 503)
                    future.set_result(self.dispatch(action))
                except Exception as error:
                    future.set_exception(error)
        self.timer = self.root.after(75, self.drain)

    def close(self):
        self.root.after_cancel(self.timer)
        self.server.shutdown()
        self.server.server_close()
