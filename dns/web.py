#!/usr/bin/env python3
"""A page listing what's running, with the sites Caddy serves from container labels.

Read-only: it only asks Docker about the running containers, the same way
sync_resolvers does. Label values are attacker-influenced, so every link is
rebuilt from a validated hostname and everything else is HTML-escaped (here)
or set as text (in static/app.js).
"""

import concurrent.futures
import html
import http.client
import http.server
import json
import os
import re
import signal
import threading
import time
from typing import NamedTuple, Optional

import diagnostics
import sync_resolvers as sr

PORT = int(os.environ.get("WEB_PORT", "8080"))
# The network caddy-docker-proxy watches; containers elsewhere are ignored by it.
CADDY_NETWORK = os.environ.get("CADDY_NETWORK", "caddy")

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
STATIC_TYPES = {"app.js": "text/javascript", "app.css": "text/css", "favicon.svg": "image/svg+xml"}

ADD_SITE = """services:
  app:
    labels:
      caddy: app.localhost
      caddy.reverse_proxy: "{{upstreams 3000}}"
    networks:
      - caddy

networks:
  caddy:
    external: true
"""

SITE_OK = "Running, and Caddy can reach it"
NOT_CADDY = "Not using Caddy, so this tool doesn't check it"


def csp(extra_connect=""):
    """Scripts and styles come only from this server (static/) and Pico's CDN, so an
    injected script or style attribute can't run. `extra_connect` widens fetch()."""
    return (
        "default-src 'none'; style-src 'self' https://cdn.jsdelivr.net; script-src 'self'; "
        "img-src 'self' data:; "  # Pico draws its checkmark and search icon as data: images
        f"connect-src 'self' {extra_connect}".rstrip()
        + "; base-uri 'none'; form-action 'self'; frame-ancestors 'none'"
    )


# ---- what is running ------------------------------------------------------

class Route(NamedTuple):
    address: str
    link: Optional[str]
    container: str
    project: str
    target: str
    problem: str  # empty when all is well
    uptime: str
    seconds: int = -1
    directory: str = ""


def site(address):
    """(display text, link or None) for one site address, or None to skip it."""
    parts = sr.split_address(address)
    if parts is None:
        return None
    scheme, host, port, wildcard = parts
    if scheme not in (None, "http", "https"):
        return None
    if not (sr.HOSTNAME.fullmatch(host) or host == "localhost"):
        return None
    if host.rsplit(".", 1)[-1].isdigit():  # an IP address
        return None
    shown = f"{scheme + '://' if scheme else ''}{'*.' if wildcard else ''}{host}"
    shown += f":{port}" if port else ""
    if wildcard:
        return shown, None
    scheme = scheme or ("http" if port == "80" else "https")
    return shown, f"{scheme}://{host}" + (f":{port}" if port else "")


def problem_with(container):
    """Why this container's sites won't work, or an empty string."""
    networks = (container.get("NetworkSettings") or {}).get("Networks") or {}
    state = container.get("State") or "running"
    if CADDY_NETWORK not in networks:
        return f"Not on the {CADDY_NETWORK} network, so Caddy can't reach it"
    if state != "running":
        return state.capitalize()
    if "(unhealthy)" in (container.get("Status") or ""):
        return "Unhealthy"
    return ""


def container_name(container):
    return ((container.get("Names") or ["?"])[0]).lstrip("/")


def uptime_of(container, details, now):
    """(text, seconds) for how long a container has been up; ('', -1) if unknown."""
    began = diagnostics.started_at((details or {}).get(container.get("Id"), {}))
    if not began or not now:
        return "", -1
    return diagnostics.humanize(now - began), int(now - began)


def routes(containers, details=None, now=None):
    """The sites found in these containers' labels. `details` maps container id
    to `docker inspect` output and supplies the uptime."""
    found = []
    for container in containers:
        labels = container.get("Labels") or {}
        uptime, seconds = uptime_of(container, details, now)
        for key in sorted(labels):
            if not sr.LABEL_KEY.fullmatch(key):
                continue
            for address in re.split(r"[\s,]+", labels[key]):
                if parsed := site(address):
                    found.append(
                        Route(
                            parsed[0],
                            parsed[1],
                            container_name(container),
                            diagnostics.compose_project(labels),
                            labels.get(f"{key}.reverse_proxy", ""),
                            problem_with(container),
                            uptime,
                            seconds,
                            labels.get(diagnostics.COMPOSE + "project.working_dir", ""),
                        )
                    )
    return sorted(found, key=lambda route: (route.address, route.container))


class Row(NamedTuple):
    """One line of the table, and one entry of /api/rows."""

    kind: str  # site, service (part of this setup) or other (not using Caddy)
    state: str  # ok, fail or none
    state_label: str
    name: str
    link: Optional[str]
    container: str
    project: str
    target: str
    problem: str
    uptime: str
    uptime_seconds: int
    directory: str
    compose_project: str = ""
    host: bool = False


def build_rows(containers, details=None, services=None, now=None):
	"""Every running container as table rows: its Caddy sites if it has any, a
	row of its own if it is one of this setup's services, otherwise a single
	grey row, since this tool doesn't look after containers Caddy doesn't proxy."""
	rows = []
	site_routes = routes(containers, details, now)
	for route in site_routes:
		rows.append(Row(
			"site", "fail" if route.problem else "ok", route.problem or SITE_OK, route.address, route.link,
			route.container, route.project, route.target, route.problem, route.uptime, route.seconds, route.directory,
		))
	site_containers = {route.container for route in site_routes}

	service_containers = set()
	for service in services or []:
		service_containers.add(service.container)
		if service.container and service.container in site_containers:
			continue
		ok = service.status == "running"
		label = "Running" if ok else service.status.capitalize()
		rows.append(Row(
			"service", "ok" if ok else "fail", label, service.service, None, service.container,
			service.project, "", "" if ok else label, service.uptime, service.seconds, service.directory,
		))

	for container in sorted(containers, key=container_name):
		name = container_name(container)
		if name in site_containers or name in service_containers:
			continue
		labels = container.get("Labels") or {}
		uptime, seconds = uptime_of(container, details, now)
		rows.append(Row(
			"other", "none", NOT_CADDY, labels.get(diagnostics.COMPOSE + "service") or name, None, name,
			diagnostics.compose_project(labels), "", "", uptime, seconds,
			labels.get(diagnostics.COMPOSE + "project.working_dir", ""),
		))
	# Keep the project separate from the display value (project/service).
	projects = {
		container_name(container): (container.get("Labels") or {}).get(diagnostics.COMPOSE + "project", "")
		for container in containers
	}
	for service in services or []:
		if service.container not in projects and "/" in service.project:
			projects[service.container] = service.project.rsplit("/", 1)[0]
	return [row._replace(compose_project=projects.get(row.container, ""), host=row.kind == "service" or row.container in service_containers) for row in rows]


def summarise(rows, domains=None):
    sites = [r for r in rows if r.kind == "site"]
    others = sum(1 for r in rows if r.kind == "other")
    text = f"{len(sites)} site{'s' if len(sites) != 1 else ''} from {len({r.container for r in sites})} container(s)"
    if domains is not None:
        text += f" · {domains} domain{'s' if domains != 1 else ''} pointed at this Mac by DNS"
    if others:
        text += f" · {others} other container{'s' if others != 1 else ''} not using Caddy"
    return text


def inspect_containers(containers):
    """`docker inspect` output by container id, for the uptime column."""

    def one(container):
        try:
            return container["Id"], sr.docker_json(f"/containers/{container['Id']}/json")
        except (OSError, ValueError, RuntimeError, KeyError):
            return None, None

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        return {ident: info for ident, info in pool.map(one, containers) if ident}


def snapshot():
    """(rows, summary) for what is running right now."""
    containers = sr.running_containers()
    stack, _ = diagnostics.collect_stack()
    now = time.time()
    services = diagnostics.service_rows(stack, now) if stack else None
    rows = build_rows(containers, inspect_containers(containers), services, now)
    domains = len(sr.hostnames([c.get("Labels") for c in containers]))
    return rows, summarise(rows, domains)


# ---- HTML -----------------------------------------------------------------

ICON_COLOURS = {"ok": "#2da44e", "warn": "#bf8700", "fail": "#d1242f", "info": "#6e7781", "none": "#8c959f"}
ICON_MARKS = {
    "ok": '<path d="M4.5 8.2l2.3 2.3 4.7-4.9"/>',
    "fail": '<path d="M5.3 5.3l5.4 5.4M10.7 5.3l-5.4 5.4"/>',
    "warn": '<path d="M8 4.2v4.4"/><circle cx="8" cy="11.3" r=".5"/>',
    "info": '<path d="M8 7.2v4.3"/><circle cx="8" cy="4.8" r=".5"/>',
    "none": '<path d="M4.8 8h6.4"/>',
}


def icon(kind, label, titled=True):
    """A round status icon. It is inline SVG with presentation attributes, since the
    page's CSP forbids inline styles; the label is for screen readers (and, if
    `titled`, a native tooltip)."""
    e = html.escape
    title = f"<title>{e(label)}</title>" if titled else ""
    return (
        f'<svg viewBox="0 0 16 16" width="18" height="18" role="img" aria-label="{e(label)}">'
        f'{title}<circle cx="8" cy="8" r="8" fill="{ICON_COLOURS[kind]}"/>'
        f'<g fill="none" stroke="#fff" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">'
        f"{ICON_MARKS[kind]}</g></svg>"
    )


def page(title, body):
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="light dark">
<title>{html.escape(title)}</title>
<link rel="icon" type="image/svg+xml" href="/static/favicon.svg">
<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/@picocss/pico@2/css/pico.min.css">
<link rel="stylesheet" href="/static/app.css">
</head>
<body>
<main class="container">
{body}
</main>
<script src="/static/app.js" defer></script>
</body>
</html>
"""


def copyable(element_id, text, label="Copy"):
    e = html.escape
    return (
        f'<pre><code id="{e(element_id)}">{e(text)}</code></pre>'
        f'<button type="button" class="secondary" data-copy-from="{e(element_id)}">{e(label)}</button>'
    )


COLUMNS = (
    ("state", "State"),
    ("name", "Site or service"),
    ("container", "Container"),
    ("project", "Compose service"),
    ("target", "Target"),
    ("uptime", "Uptime"),
)


def render_row(row):
    """Server-side first paint and the no-JavaScript fallback. static/app.js builds
    the same cells from /api/rows, so keep the two in step."""
    e = html.escape
    state = f'<span class="tip" data-tip="{e(row.state_label)}" title="{e(row.state_label)}" tabindex="0">{icon(row.state, row.state_label, titled=False)}</span>'
    name = e(row.name)
    if row.link:
        name = f'<a href="{e(row.link)}" rel="noreferrer">{name}</a>'
    if row.problem:
        name += f"<br><small>{e(row.problem)}</small>"
    container = e(row.container)
    target = f"<code>{e(row.target)}</code>" if row.target else ""
    return (
        f"<tr><td>{state}</td><td>{name}</td><td>{container}</td><td>{e(row.project)}</td>"
        f'<td>{target}</td><td class="uptime">{e(row.uptime)}</td></tr>'
    )


def copy_path_button(directory=""):
	return (
		f'<button type="button" class="copy-path" data-copy-path="{html.escape(directory)}" '
		'aria-label="Copy folder path" title="Copy folder path">'
		'<svg viewBox="0 0 16 16" width="16" height="16" aria-hidden="true" fill="none" '
		'stroke="currentColor" stroke-width="1.5" stroke-linejoin="round">'
		'<rect x="5.5" y="5.5" width="8" height="8" rx="1"/>'
		'<path d="M10.5 5.5v-3h-8v8h3"/></svg></button>'
	)


def grouped_rows(rows):
	"""Group a Compose project's services, distinguishing checkouts by folder."""
	groups = {}
	for row in rows:
		key = (row.compose_project, row.directory) if row.compose_project else ("", "")
		groups.setdefault(key, []).append(row)
	parts = []
	for (project, directory), members in sorted(groups.items(), key=lambda item: (not item[0][0], item[0])):
		label = html.escape(project or "Standalone containers")
		folder = f'<span class="group-folder"><small>{html.escape(directory)}</small>{copy_path_button(directory)}</span>' if project and directory else ""
		count = len({row.container or row.name for row in members})
		parts.append(
			'<tr class="compose-group"><th scope="rowgroup" colspan="6"><div class="group-heading">'
			f'<span>{label} <small>· {count} container(s)</small></span>{folder}</div></th></tr>'
		)
		parts.extend(render_row(row) for row in members)
	return "".join(parts)


def render(rows, summary=""):
	e = html.escape
	heads = "".join(f'<th scope="col" data-sort="{key}" tabindex="0" aria-sort="none">{label}</th>' for key, label in COLUMNS)
	if rows:
		table = (
			'<div class="toolbar"><input type="search" id="filter" placeholder="Search sites, containers, folders..." '
			'aria-label="Search the table">'
			'<label><input type="checkbox" id="show-other"> Show containers not using Caddy</label>'
			'<label><input type="checkbox" id="group-compose" checked> Group by Compose project</label>'
			'<label><input type="checkbox" id="hide-host" checked> Hide host containers</label>'
			'<small id="count"></small><small id="copy-status" role="status"></small></div>'
			f'<div class="overflow-auto"><table id="routes"><thead><tr>{heads}</tr></thead>'
			f"<tbody>{grouped_rows(rows)}</tbody></table></div>"
			'<p id="no-match" hidden>Nothing matches your search.</p>'
			'<small id="updated" aria-live="polite">Updates by itself every few seconds.</small>'
		)
	else:
		table = "<p>No containers are running.</p>"
	icons = "".join(f'<template data-icon="{kind}">{icon(kind, "", titled=False)}</template>' for kind in ("ok", "fail", "none"))

	body = f"""<hgroup>
<h1>Routes</h1>
<p>What's running, and the sites Caddy serves from container labels.</p>
</hgroup>
<h2>What's running</h2>
<p id="summary">{e(summary)}</p>
{table}
{icons}
<template id="copy-path-icon">{copy_path_button()}</template>
<h2>Add a site</h2>
<label for="site-hostname">Hostname</label>
<input type="text" id="site-hostname" value="app.localhost" placeholder="app.localhost"
	autocomplete="off" spellcheck="false" aria-describedby="site-hostname-help">
<small id="site-hostname-help">Enter a hostname, such as shop.localhost or app.example.test, without https:// or a path. Leave blank to use app.localhost.</small>
<label for="site-port">Container port</label>
<input type="number" id="site-port" value="3000" placeholder="3000" min="1" max="65535" step="1"
	aria-describedby="site-port-help">
<small id="site-port-help">Use the port your app listens on inside the container (1–65535). Caddy reaches it over the shared Docker network, so you don't need a <code>ports:</code> mapping or an <code>expose:</code> entry. Leave blank to use 3000.</small>
<ol>
<li>Open the <code>docker-compose.yml</code> of the project you want to add. Put this in it, changing
<code>app</code> to your service's name.</li>
{copyable("add-site", ADD_SITE)}
<li>Restart that project:</li>
{copyable("restart-cmd", "docker compose up -d")}
<li>Your site appears in the table above, and its address should open in your browser.</li>
</ol>
<p><small>Any domain works, not only <code>.localhost</code>: use <code>ridiculous.com</code> and this Mac will
send it to your container. Keep the <code>caddy.reverse_proxy</code> line, because a site with no directive can stop every site.</small></p>
<h2>Something not working?</h2>
<form method="get" action="/diagnose"><button type="submit">Run checks</button></form>
<p><small>If this page itself won't load, run <code>just doctor</code> in a terminal instead.</small></p>"""
	return page("Routes", body)


CHECK_LABELS = {"ok": "OK", "warn": "Warning", "fail": "Problem", "info": "Note"}


def render_diagnosis(checks, found):
    e = html.escape
    items = []
    for check in checks:
        detail = f"<br><small>{e(check.detail).replace(chr(10), '<br>')}</small>" if check.detail else ""
        fix = f"<br><small><strong>Fix:</strong> {e(check.fix)}</small>" if check.fix else ""
        items.append(
            f"<tr><td>{icon(check.status, CHECK_LABELS[check.status])}</td>"
            f"<td>{e(check.title)}{detail}{fix}</td></tr>"
        )
    problems = sum(1 for c in checks if c.status in ("fail", "warn"))
    verdict = "Everything looks fine." if not problems else f"{problems} thing{'s' if problems != 1 else ''} to look at."

    browser_rows = "".join(
        f'<tr data-url="{e(r.link)}"><td>{e(r.address)}</td><td class="result"></td></tr>'
        for r in found
        if r.link and r.link.startswith("https://")
    )
    browser = ""
    if browser_rows:
        browser = (
            "<h2>From this browser</h2>"
            "<p>Opens each site the way you would. If one can't be opened, its name may not resolve "
            "(clear the DNS cache with <code>just flush-dns</code>), its certificate may not be trusted "
            "(<code>just trust</code>), or Caddy may be down (<code>just doctor</code>).</p>"
            f'<table><thead><tr><th>Site</th><th>Result</th></tr></thead><tbody>{browser_rows}</tbody></table>'
        )

    body = f"""<h1>Checks</h1>
<p>{e(verdict)}</p>
<table><tbody>{"".join(items)}</tbody></table>
{browser}
<h2>Clear this Mac's DNS cache</h2>
<p>A page that is stuck on an old or wrong address, though the checks above pass, is usually the Mac's cache. A web page can't do this for you, so run this in a terminal (it asks for your password):</p>
{copyable("flush-cmd", "just flush-dns")}
<p><a href="/">Back to the routes</a> &middot; <a href="/diagnose">Run the checks again</a></p>"""
    return page("Checks", body)


# ---- serving --------------------------------------------------------------

class Handler(http.server.BaseHTTPRequestHandler):
    timeout = 10

    def do_GET(self):
        self.respond(send_body=True)

    def do_HEAD(self):
        self.respond(send_body=False)

    def respond(self, send_body):
        path = self.path.split("?", 1)[0]
        if path.startswith("/static/"):
            return self.static(path[len("/static/"):], send_body)
        if path not in ("/", "/diagnose", "/api/rows"):
            return self.reply(404, "not found\n", "text/plain", send_body)
        try:
            if path == "/":
                rows, summary = snapshot()
                return self.reply(200, render(rows, summary), "text/html", send_body, csp())
            if path == "/api/rows":
                rows, summary = snapshot()
                data = json.dumps({"summary": summary, "rows": [row._asdict() for row in rows]})
                return self.reply(200, data, "application/json", send_body)
            snap = diagnostics.collect()
            text = render_diagnosis(diagnostics.run(snap), routes(snap["containers"]))
            self.reply(200, text, "text/html", send_body, csp("https:"))
        except (OSError, http.client.HTTPException, ValueError, RuntimeError) as error:
            sr.log(f"couldn't list containers: {error}")
            self.reply(502, "Couldn't reach Docker\n", "text/plain", send_body)

    def static(self, name, send_body):
        if name not in STATIC_TYPES:
            return self.reply(404, "not found\n", "text/plain", send_body)
        with open(os.path.join(STATIC_DIR, name), "rb") as file:
            self.reply(200, file.read(), STATIC_TYPES[name], send_body)

    def reply(self, status, body, content_type, send_body, policy="default-src 'none'"):
        if isinstance(body, str):
            body = body.encode()
        self.send_response(status)
        self.send_header("Content-Type", f"{content_type}; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Security-Policy", policy)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()
        if send_body:
            self.wfile.write(body)


def main():
    server = http.server.ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    server.daemon_threads = True
    signal.signal(signal.SIGTERM, lambda *_: threading.Thread(target=server.shutdown).start())
    server.serve_forever()


if __name__ == "__main__":
    main()
