"""Builders and fakes shared by the tests."""

import json
import socketserver
import time
from http.server import BaseHTTPRequestHandler

import diagnostics as d


def web_container(labels, name="/app-1", networks=("caddy",)):
    """A running container as `docker ps` reports it (labels first), for the routes page."""
    return {
        "Names": [name],
        "Labels": labels,
        "NetworkSettings": {"Networks": {network: {} for network in networks}},
    }


def stack_container(name, labels, networks=("caddy",), ident=None):
    """A running container with an IP on each network (name first), for the checks."""
    return {
        "Id": ident or name,
        "Names": [f"/{name}"],
        "Labels": labels,
        "NetworkSettings": {"Networks": {n: {"IPAddress": "172.20.0.9"} for n in networks}},
    }


def running(started="2026-10-18T10:00:00.5Z", restarts=0, status="running", mounts=()):
    """`docker inspect` output for one of the stack's own services."""
    return {
        "State": {"Status": status, "StartedAt": started},
        "RestartCount": restarts,
        "Mounts": [{"Destination": m} for m in mounts],
    }


def healthy(**changes):
    """A snapshot of a stack with nothing wrong, with any part replaced by `changes`."""
    snap = {
        "now": 1_792_000_000,
        "containers": [
            stack_container("shop-web-1", {"caddy": "shop.localhost", "caddy.reverse_proxy": "{{upstreams 3000}}"})
        ],
        "stack": {name: running(mounts=["/etc/caddy/Caddyfile"] if name == "caddy" else []) for name in d.EXPECTED_SERVICES},
        "project": "caddy-local",
        "caddy_logs": "",
        "dns_answer": ["127.0.0.1"],
        "resolver_files": set(),
        "probes": {("shop-web-1", 3000): True},
    }
    snap.update(changes)
    return snap


def titles(checks, status=None):
    return [c.title for c in checks if status is None or c.status == status]


class FakeDocker(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True

    def __init__(self, path):
        self.labels = []
        self.stall = False
        super().__init__(path, Handler)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def address_string(self):
        return "unix"

    def do_GET(self):
        if self.path.startswith("/events"):
            self.send_response(200)
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            self.wfile.flush()
            time.sleep(60)
            return
        body = json.dumps([{"Labels": labels} for labels in self.server.labels]).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(body) + 100000))
        self.end_headers()
        self.wfile.write(body)
        self.wfile.flush()
        try:
            while self.server.stall:  # trickle forever, like a wedged daemon
                time.sleep(0.5)
                self.wfile.write(b" ")
                self.wfile.flush()
        except OSError:
            pass
        self.close_connection = True
