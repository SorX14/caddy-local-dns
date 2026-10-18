#!/usr/bin/env python3
"""Checks for the usual ways this setup goes wrong, each with a plain fix.

collect() gathers what's needed from Docker, the dns service and the resolver
directory. run() turns that into a list of Check results without doing any I/O,
so it can be tested with made-up data. Run as a script it prints the results;
`just doctor` does that, because the routes page is served by Caddy and so is
down in exactly the cases where something is badly wrong.
"""

import calendar
import concurrent.futures
import json
import os
import re
import socket
import struct
import sys
import time
import urllib.parse
from typing import NamedTuple

import sync_resolvers as sr

CADDY_NETWORK = os.environ.get("CADDY_NETWORK", "caddy")
DNS_HOST = os.environ.get("DNS_HOST", "dns")
DNS_INTERNAL_PORT = 5300  # what dnsmasq.conf listens on, inside the container
EXPECTED_SERVICES = ("caddy", "dns", "dns-sync", "dns-web")
LOG_WINDOW = 600  # seconds of Caddy log to look at
PROBE_TIMEOUT = 1.0

UPSTREAMS = re.compile(r"\{\{\s*upstreams(?:\s+(?:https?|h2c))?(?:\s+(\d+))?\s*\}\}")


class Check(NamedTuple):
    status: str  # ok, warn, fail or info
    title: str
    detail: str = ""
    fix: str = ""


def humanize(seconds):
    seconds = int(max(seconds, 0))
    for unit, size in (("day", 86400), ("h", 3600), ("min", 60)):
        if seconds >= size:
            first, rest = divmod(seconds, size)
            if unit == "day":
                return f"{first} day{'s' if first != 1 else ''} {rest // 3600} h"
            if unit == "h":
                return f"{first} h {rest // 60} min"
            return f"{first} min"
    return f"{seconds} s"


def started_at(info):
    """Epoch seconds from Docker's StartedAt, or None."""
    match = re.match(r"(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)", (info.get("State") or {}).get("StartedAt", ""))
    if not match or match[1].startswith("0001"):
        return None
    return calendar.timegm(time.strptime(match[1], "%Y-%m-%dT%H:%M:%S"))


# ---- reading things -------------------------------------------------------

def demux_logs(raw):
    """Text from Docker's log stream, which frames each chunk with an 8-byte
    header unless the container has a TTY."""
    if not raw or raw[0] not in (0, 1, 2):
        return raw.decode(errors="replace")
    out, i = [], 0
    while i + 8 <= len(raw):
        size = struct.unpack(">I", raw[i + 4:i + 8])[0]
        out.append(raw[i + 8:i + 8 + size].decode(errors="replace"))
        i += 8 + size
    return "".join(out)


def query_a(host, port, name, timeout=2.0):
    """The IPv4 addresses a DNS server gives for `name`, using a minimal query."""
    ident = os.urandom(2)
    question = b"".join(bytes([len(part)]) + part.encode() for part in name.split(".")) + b"\0\0\1\0\1"
    packet = ident + b"\1\0\0\1\0\0\0\0\0\0" + question
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.settimeout(timeout)
        sock.sendto(packet, (host, port))
        data, _ = sock.recvfrom(1500)
    if data[:2] != ident or len(data) < 12:
        raise OSError("unexpected DNS reply")
    rcode, count = data[3] & 15, struct.unpack(">H", data[6:8])[0]
    if rcode:
        raise OSError(f"DNS server answered with error code {rcode}")
    pos = 12 + len(question)
    addresses = []
    for _ in range(count):
        while data[pos] and not data[pos] & 0xC0:  # skip the name
            pos += data[pos] + 1
        pos += 2 if data[pos] & 0xC0 else 1
        kind, _, _, length = struct.unpack(">HHIH", data[pos:pos + 10])
        pos += 10
        if kind == 1 and length == 4:
            addresses.append(".".join(str(b) for b in data[pos:pos + 4]))
        pos += length
    return addresses


def site_families(labels):
    """{label key: [addresses]} for the `caddy` / `caddy_N` labels."""
    families = {}
    for key, value in (labels or {}).items():
        if sr.LABEL_KEY.fullmatch(key):
            families[key] = [a for a in re.split(r"[\s,]+", value) if a]
    return families


def upstream_ports(container):
    """Ports named by `{{upstreams PORT}}` in this container's reverse_proxy labels."""
    labels = container.get("Labels") or {}
    ports = set()
    for key in site_families(labels):
        match = UPSTREAMS.search(labels.get(f"{key}.reverse_proxy", ""))
        if match and match[1]:
            ports.add(int(match[1]))
    return ports


def container_ip(container):
    networks = (container.get("NetworkSettings") or {}).get("Networks") or {}
    return (networks.get(CADDY_NETWORK) or {}).get("IPAddress") or None


def probe(ip, port):
    try:
        with socket.create_connection((ip, port), timeout=PROBE_TIMEOUT):
            return True
    except OSError:
        return False


def collect_stack():
    """{service: docker inspect} for this compose project's own services, and
    the project name. Empty if we can't tell which project we are."""
    stack = {}
    try:
        own = sr.docker_json(f"/containers/{os.environ.get('HOSTNAME', '')}/json")
        project = (own.get("Config") or {}).get("Labels", {}).get("com.docker.compose.project")
        if not project:
            return {}, None
        filters = json.dumps({"label": [f"com.docker.compose.project={project}"]})
        listing = sr.docker_json("/containers/json?all=1&filters=" + urllib.parse.quote(filters))
        for container in listing:
            service = (container.get("Labels") or {}).get("com.docker.compose.service")
            if service in EXPECTED_SERVICES and service not in stack:
                stack[service] = sr.docker_json(f"/containers/{container['Id']}/json")
        return stack, project
    except (OSError, ValueError, RuntimeError, KeyError):
        return {}, None


def collect():
    snap = {"now": time.time(), "problems": []}
    snap["containers"] = sr.running_containers()
    snap["stack"], snap["project"] = collect_stack()

    caddy = snap["stack"].get("caddy")
    snap["caddy_logs"] = None
    if caddy:
        try:
            path = f"/containers/{caddy['Id']}/logs?stdout=1&stderr=1&tail=500&since={int(snap['now'] - LOG_WINDOW)}"
            snap["caddy_logs"] = demux_logs(sr.docker_body(path, max_bytes=8 << 20))
        except (OSError, RuntimeError, ValueError):
            pass

    try:
        snap["dns_answer"] = query_a(DNS_HOST, DNS_INTERNAL_PORT, "doctor.test")
    except (OSError, IndexError, struct.error) as error:
        snap["dns_answer"] = str(error) or error.__class__.__name__

    try:
        names = os.listdir(sr.RESOLVER_DIR)
        snap["resolver_files"] = {n for n in names if sr.read_managed(os.path.join(sr.RESOLVER_DIR, n)) is not None}
    except OSError:
        snap["resolver_files"] = None

    targets = {(c["Id"], port): (container_ip(c), port) for c in snap["containers"] for port in upstream_ports(c)}
    snap["probes"] = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        futures = {key: pool.submit(probe, ip, port) for key, (ip, port) in targets.items() if ip}
        for key, future in futures.items():
            snap["probes"][key] = future.result()
    return snap


# ---- judging them ---------------------------------------------------------

def name_of(container):
    return ((container.get("Names") or ["?"])[0]).lstrip("/")


class ServiceRow(NamedTuple):
    service: str
    status: str
    uptime: str
    restarts: int
    container: str
    project: str = ""
    directory: str = ""
    seconds: int = -1  # uptime in seconds, -1 when unknown


COMPOSE = "com.docker.compose."


def compose_project(labels):
    """`project/service` for a container started by Docker Compose, else ''."""
    return "/".join(filter(None, [(labels or {}).get(COMPOSE + "project"), (labels or {}).get(COMPOSE + "service")]))


def service_rows(stack, now):
    """One row per service in the stack, whether or not it is running."""
    rows = []
    for service in EXPECTED_SERVICES:
        info = stack.get(service)
        if info is None:
            rows.append(ServiceRow(service, "not running", "", 0, ""))
            continue
        status = (info.get("State") or {}).get("Status", "unknown")
        began = started_at(info)
        up = status == "running" and began
        labels = (info.get("Config") or {}).get("Labels") or {}
        rows.append(ServiceRow(
            service,
            status,
            humanize(now - began) if up else "",
            info.get("RestartCount", 0),
            (info.get("Name") or "").lstrip("/"),
            compose_project(labels),
            labels.get(COMPOSE + "project.working_dir", ""),
            int(now - began) if up else -1,
        ))
    return rows


def check_services(snap):
    if not snap["stack"]:
        return [Check("info", "Couldn't work out which services belong to this stack", "Skipping the service checks.")]
    checks = []
    for row in service_rows(snap["stack"], snap["now"]):
        if row.status != "running":
            checks.append(Check("fail", f"{row.service} is {row.status}", fix=f"Run `just up`, then `just logs {row.service}` if it keeps stopping."))
        elif row.restarts > 2:
            checks.append(Check("warn", f"{row.service} has restarted {row.restarts} times", fix=f"Run `just logs {row.service}` to see why."))
        else:
            checks.append(Check("ok", f"{row.service} is running", f"Up {row.uptime}."))
    return checks


def check_caddyfile(snap):
    caddy = snap["stack"].get("caddy")
    if not caddy:
        return []
    mounted = any(m.get("Destination") == "/etc/caddy/Caddyfile" for m in caddy.get("Mounts") or [])
    if mounted:
        return [Check("ok", "Caddy is using the Caddyfile")]
    return [Check(
        "fail",
        "Caddy isn't using the Caddyfile",
        "Without it, sites with real domain names try to get certificates from a public authority and fail.",
        "Restore the ./Caddyfile mount and CADDY_DOCKER_CADDYFILE_PATH in compose.yml, then run `just up`.",
    )]


def check_labels(snap):
    checks, owners, sites = [], {}, 0
    containers = [c for c in snap["containers"] if site_families(c.get("Labels"))]
    for container in containers:
        labels, name = container.get("Labels") or {}, name_of(container)
        networks = (container.get("NetworkSettings") or {}).get("Networks") or {}
        for key, addresses in site_families(labels).items():
            if not addresses:
                continue
            sites += len(addresses)
            for address in addresses:
                host = sr.split_address(address)
                if host:
                    owners.setdefault(host[1], set()).add(name)
            if not any(k.startswith(key + ".") for k in labels):
                checks.append(Check(
                    "fail",
                    f"{name}: {' '.join(addresses)} has no directive",
                    "Caddy refuses a site that does nothing, and one bad site can stop every site from working.",
                    f'Add a directive to that container, for example {key}.reverse_proxy: "{{{{upstreams PORT}}}}", or remove the container.',
                ))
        if CADDY_NETWORK not in networks:
            checks.append(Check(
                "warn",
                f"{name} isn't on the {CADDY_NETWORK} network",
                "Caddy can't reach it.",
                f"List the {CADDY_NETWORK} network under that service's networks in its docker-compose.yml, then run `docker compose up -d`.",
            ))
    for host, names in sorted(owners.items()):
        if len(names) > 1:
            checks.append(Check("warn", f"{host} is claimed by {len(names)} containers", ", ".join(sorted(names)), "Give each container its own address."))
    if not containers:
        return [Check("info", "No containers have a caddy label yet", fix="See Add a site on the routes page.")]
    if not any(c.status in ("fail", "warn") for c in checks):
        checks.append(Check("ok", f"{sites} site{'s' if sites != 1 else ''} in {len(containers)} container{'s' if len(containers) != 1 else ''} look right"))
    return checks


def check_upstreams(snap):
    checks, probed = [], 0
    for container in snap["containers"]:
        for port in sorted(upstream_ports(container)):
            result = snap["probes"].get((container["Id"], port))
            if result is None:
                continue
            probed += 1
            if not result:
                checks.append(Check(
                    "warn",
                    f"Nothing answers on port {port} in {name_of(container)}",
                    "Caddy will show 502 Bad Gateway for its sites.",
                    f"Use the port the app listens on inside the container in {{{{upstreams {port}}}}}. Make sure the app is running and listening on all addresses (0.0.0.0), not only 127.0.0.1.",
                ))
    if probed and not checks:
        checks.append(Check("ok", f"Every upstream port answered ({probed})"))
    return checks


CADDY_ERROR_HINTS = (
    ("subject does not qualify", "A site name that can't get a certificate (a typo, a trailing dot or a bad character). Check the names in your labels."),
    ("acme-v02", "Caddy is trying to get a public certificate. The Caddyfile with local_certs probably isn't loaded."),
    ("unrecognized directive", "A site label has no directive or a typo in one. See the failing checks above."),
    ("Removing invalid block", "Caddy dropped a site it couldn't read. See the failing checks above."),
)


def caddy_error_lines(text):
    seen, found = set(), []
    for line in (text or "").splitlines():
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        message = str(entry.get("msg", ""))
        logs = str(entry.get("logs", ""))
        if entry.get("level") == "error" or "[ERROR]" in logs:
            summary = (logs.strip() or message).splitlines()[0][:200]
            if summary not in seen:
                seen.add(summary)
                found.append(summary)
    return found


def check_caddy_errors(snap):
    if snap.get("caddy_logs") is None:
        return []
    errors = caddy_error_lines(snap["caddy_logs"])
    if not errors:
        return [Check("ok", "Caddy hasn't logged errors recently")]
    hint = next((h for needle, h in CADDY_ERROR_HINTS if any(needle in e for e in errors)), "")
    return [Check(
        "warn",
        f"Caddy logged {len(errors)} different error{'s' if len(errors) != 1 else ''} in the last {LOG_WINDOW // 60} minutes",
        "\n".join(errors[:3]),
        hint or "Run `just logs caddy` for the full story.",
    )]


def check_dns(snap):
    answer = snap["dns_answer"]
    checks = []
    if isinstance(answer, list) and "127.0.0.1" in answer:
        checks.append(Check("ok", "The dns service is answering"))
    else:
        checks.append(Check("fail", "The dns service isn't answering", str(answer), "Run `just logs dns`, then `just restart`."))

    wanted = sr.hostnames([c.get("Labels") for c in snap["containers"]])
    present = snap["resolver_files"]
    if present is None:
        checks.append(Check("warn", "Can't read the resolver directory", "", "Run `just up` to set it up."))
    else:
        missing, stale = sorted(wanted - present), sorted(present - wanted)
        if missing:
            checks.append(Check("fail", f"No DNS entry yet for {', '.join(missing)}", "Those names won't open in a browser.", "Run `just logs dns-sync` to see why."))
        if stale:
            checks.append(Check("warn", f"Leftover DNS entries: {', '.join(stale)}", "", "Run `just dns-clear`."))
        if not missing and not stale:
            checks.append(Check("ok", f"{len(wanted)} domain{'s' if len(wanted) != 1 else ''} point{'s' if len(wanted) == 1 else ''} at this Mac" if wanted else "No domains need DNS entries"))
    return checks


def run(snap):
    checks = []
    for part in (check_services, check_caddyfile, check_labels, check_upstreams, check_caddy_errors, check_dns):
        checks += part(snap)
    return checks


FLUSH_HINT = "A name still opens the wrong page or nothing, though everything above is fine? Clear this Mac's DNS cache with `just flush-dns`."


def main():
    checks = run(collect())
    tags = {"ok": "ok  ", "warn": "warn", "fail": "FAIL", "info": "info"}
    for check in checks:
        print(f"[{tags[check.status]}] {check.title}")
        for line in check.detail.splitlines():
            print(f"       {line}")
        if check.fix:
            print(f"       Fix: {check.fix}")
    print(f"\n{FLUSH_HINT}")
    return 1 if any(c.status == "fail" for c in checks) else 0


if __name__ == "__main__":
    sys.exit(main())
