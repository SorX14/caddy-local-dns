#!/usr/bin/env python3
"""Keep one macOS /etc/resolver file per domain named in a container's `caddy`
label, so those domains resolve through the dns service.

Files this didn't write (no marker line, symlinks, anything not a regular
file) are never touched. If Docker can't be queried, existing files are left
as they are.
"""

import http.client
import json
import os
import re
import select
import signal
import socket
import stat
import threading
import time
import urllib.parse

DOCKER_SOCKET = os.environ.get("DOCKER_SOCKET", "/var/run/docker.sock")
RESOLVER_DIR = os.environ.get("RESOLVER_DIR", "/resolver")
PORT = os.environ.get("DNS_PORT", "5300")
INTERVAL = float(os.environ.get("SYNC_INTERVAL", "30"))

# Docker has to answer within this long overall, and each read within RECV_TIMEOUT,
# so a stalled daemon can't hold up shutdown past compose's 10s grace period.
SNAPSHOT_TIMEOUT = 5
RECV_TIMEOUT = 2

MARKER = "# managed by caddy-local"
CONTENT = f"{MARKER}\nnameserver 127.0.0.1\nport {PORT}\n"

LABEL_KEY = re.compile(r"caddy(_[0-9]+)?")
DNS_LABEL = r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
HOSTNAME = re.compile(rf"{DNS_LABEL}(?:\.{DNS_LABEL})+")

# Claiming one of these would hijack unrelated sites (or reverse lookups, or
# mDNS) for the whole Mac. Names *under* them, like app.github.io, are fine.
# There's no public-suffix list in here, so this only covers the obvious ones.
DENIED_SUFFIXES = ("arpa", "local")
DENIED_NAMES = frozenset(
    """co.uk org.uk ac.uk gov.uk com.au net.au org.au co.nz co.jp co.in co.za com.br
    github.io gitlab.io herokuapp.com vercel.app netlify.app pages.dev workers.dev
    appspot.com azurewebsites.net cloudfront.net amazonaws.com blogspot.com""".split()
)

stopping = False


def log(message):
    print(f"sync-resolvers: {message}", flush=True)


def split_address(address):
    """(scheme, host, port, wildcard) for one Caddy site address, or None.

    The scheme and port are None when absent, a leading `*.` is reported as
    `wildcard`, paths are dropped, and internationalised names become punycode
    (which is what Caddy serves). The host is not validated beyond its length.
    """
    host = address.lower()
    scheme = port = None
    if match := re.match(r"([a-z][a-z0-9+.-]*)://", host):
        scheme, host = match[1], host[match.end():]
    host = re.split(r"[/?]", host, maxsplit=1)[0]
    if match := re.search(r":([0-9]+)$", host):
        port, host = match[1], host[:match.start()]
    wildcard = host.startswith("*.")
    host = host.removeprefix("*.")
    if not host.isascii():
        try:
            host = host.encode("idna").decode("ascii")
        except UnicodeError:
            return None
    if len(host) > 253:
        return None
    return scheme, host, port, wildcard


def hostname(address):
    """The DNS name to claim for one Caddy site address, or None.

    A wildcard collapses to its parent, since a resolver file already covers
    subdomains. Single-label names (which would claim a whole TLD), IP
    addresses, placeholders, *.localhost (which macOS resolves itself) and the
    names in DENIED_* are rejected.
    """
    parts = split_address(address)
    if parts is None:
        return None
    host = parts[1]
    if not HOSTNAME.fullmatch(host):
        return None
    if host.endswith(".localhost") or host.rsplit(".", 1)[1].isdigit():
        return None
    if host in DENIED_NAMES or host.rsplit(".", 1)[1] in DENIED_SUFFIXES:
        return None
    return host


def hostnames(label_sets):
    found = set()
    for labels in label_sets:
        for key, value in (labels or {}).items():
            if LABEL_KEY.fullmatch(key):
                for address in re.split(r"[\s,]+", value):
                    if name := hostname(address):
                        found.add(name)
    return found


class DockerConnection(http.client.HTTPConnection):
    def __init__(self, timeout=None):
        super().__init__("localhost", timeout=timeout)

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(DOCKER_SOCKET)


def docker_get(path, timeout=None):
    conn = DockerConnection(timeout)
    try:
        conn.request("GET", path)
        response = conn.getresponse()
        if response.status != 200:
            raise RuntimeError(f"docker returned {response.status} for {path}")
    except BaseException:
        conn.close()
        raise
    return conn, response


def docker_body(path, max_bytes=64 << 20):
    """The raw body of a GET to the Docker API, with an overall deadline so a
    daemon that trickles data can't hold the caller up."""
    deadline = time.monotonic() + SNAPSHOT_TIMEOUT
    conn, response = docker_get(path, timeout=RECV_TIMEOUT)
    try:
        body = b""
        while chunk := response.read1(65536):
            body += chunk
            if stopping:
                raise RuntimeError("shutting down")
            if time.monotonic() > deadline or len(body) > max_bytes:
                raise RuntimeError("docker is too slow or sent too much")
        return body
    finally:
        conn.close()


def docker_json(path):
    return json.loads(docker_body(path))


def running_containers():
    return docker_json("/containers/json")


def running_labels():
    return [container.get("Labels") for container in running_containers()]


def read_managed(path):
    """The file's text if it's a regular file we wrote, else None."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        return None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return None
        text = os.read(fd, 4096).decode(errors="replace")
    except OSError:
        return None
    finally:
        os.close(fd)
    return text if text.split("\n", 1)[0] == MARKER else None


def write_resolver(name, warned):
    path = os.path.join(RESOLVER_DIR, name)
    temp = os.path.join(RESOLVER_DIR, f".{name}.tmp")
    try:
        if os.path.lexists(path):
            text = read_managed(path)
            if text is None:
                if name not in warned:
                    warned.add(name)
                    log(f"{path} exists and isn't managed here, leaving it alone")
                return
            if text == CONTENT:
                return
        # Write beside it and rename, so a reader never sees a partial file.
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o644)
        with os.fdopen(fd, "w") as file:
            file.write(CONTENT)
        os.replace(temp, path)
        log(f"added {name}")
    except OSError as error:
        log(f"couldn't write {path}: {error}")


def remove_managed(keep):
    """Delete managed files for names not in `keep`. Our names are lowercase,
    but APFS is case-insensitive, so a differently-cased file for a wanted name
    is the same file and stays."""
    try:
        names = os.listdir(RESOLVER_DIR)
    except OSError as error:
        log(f"couldn't read {RESOLVER_DIR}: {error}")
        return
    for name in names:
        if name.lower() in keep:
            continue
        path = os.path.join(RESOLVER_DIR, name)
        if read_managed(path) is not None:
            try:
                os.unlink(path)
                log(f"removed {name}")
            except OSError as error:
                log(f"couldn't remove {path}: {error}")


class Syncer:
    def __init__(self):
        self.docker_ok = True
        self.warned = set()

    def reconcile(self):
        try:
            wanted = hostnames(running_labels())
        except (OSError, http.client.HTTPException, ValueError, RuntimeError) as error:
            if self.docker_ok:
                log(f"couldn't list containers ({error}), keeping existing files")
            self.docker_ok = False
            return
        if not self.docker_ok:
            log("docker is back")
        self.docker_ok = True

        for name in sorted(wanted):
            write_resolver(name, self.warned)
        remove_managed(wanted)


class Wakeup:
    """A self-pipe rather than threading.Event, because a signal handler that
    takes the Event's lock can deadlock against the main thread holding it."""

    def __init__(self):
        self.reader, self.writer = os.pipe()
        os.set_blocking(self.reader, False)
        os.set_blocking(self.writer, False)

    def set(self):
        try:
            os.write(self.writer, b"x")
        except BlockingIOError:
            pass

    def wait(self, timeout):
        select.select([self.reader], [], [], timeout)
        try:
            while os.read(self.reader, 4096):
                pass
        except BlockingIOError:
            pass


def watch_events(wakeup):
    """Wake the main loop on every container start/die, and whenever the
    stream (re)connects so anything missed in the gap is picked up."""
    filters = json.dumps({"type": ["container"], "event": ["start", "die"]})
    path = "/events?" + urllib.parse.urlencode({"filters": filters})
    last_error = None
    while True:
        try:
            conn, response = docker_get(path)
            last_error = None
            wakeup.set()
            while response.readline():
                wakeup.set()
            conn.close()
        except (OSError, http.client.HTTPException, RuntimeError) as error:
            if str(error) != last_error:
                log(f"event stream unavailable: {error}")
                last_error = str(error)
        time.sleep(5)


def main():
    wakeup = Wakeup()

    def on_signal(*_):
        global stopping
        stopping = True
        wakeup.set()

    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGINT, on_signal)
    threading.Thread(target=watch_events, args=(wakeup,), daemon=True).start()

    syncer = Syncer()
    try:
        while not stopping:
            try:
                syncer.reconcile()
            except Exception as error:  # one bad entry must not end the loop
                log(f"reconcile failed: {error!r}")
            wakeup.wait(INTERVAL)
    finally:
        # Take the files with us, so domains don't point at a stopped DNS server.
        remove_managed(set())


if __name__ == "__main__":
    main()
