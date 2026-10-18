"""The routes page served over real HTTP (Docker faked)."""

import json
import os
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from unittest import mock

import diagnostics
import sync_resolvers as sr
import web
from tests import DNS_DIR
from tests.support import healthy
from tests.support import web_container as container


class Server(unittest.TestCase):
    def setUp(self):
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), web.Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def fetch(self, path="/", method="GET"):
        request = urllib.request.Request(self.url + path, method=method)
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                return response.status, dict(response.headers), response.read()
        except urllib.error.HTTPError as error:
            return error.code, dict(error.headers), error.read()

    def docker(self, containers, stack=None):
        """Fake the Docker side: the running containers, and (optionally) the stack."""
        self.addCleanup(mock.patch.stopall)
        mock.patch.object(sr, "running_containers", return_value=containers).start()
        mock.patch.object(diagnostics, "collect_stack", return_value=(stack or {}, "p" if stack else None)).start()
        mock.patch.object(web, "inspect_containers", return_value={}).start()


class Serving(Server):
    def test_page(self):
        self.docker([container({"caddy": "a.localhost"})])
        status, headers, body = self.fetch("/?x=1")
        self.assertEqual(status, 200)
        self.assertIn("a.localhost", body.decode())
        self.assertTrue(headers["Content-Type"].startswith("text/html"))
        self.assertEqual(headers["Cache-Control"], "no-store")

    def test_head_has_no_body(self):
        self.docker([])
        status, headers, body = self.fetch(method="HEAD")
        self.assertEqual((status, body), (200, b""))
        self.assertGreater(int(headers["Content-Length"]), 0)

    def test_other_paths_and_methods(self):
        self.assertEqual(self.fetch("/etc/passwd")[0], 404)
        self.assertEqual(self.fetch("/", method="POST")[0], 501)

    def test_docker_down_is_a_502_not_a_crash(self):
        with mock.patch.object(sr, "running_containers", side_effect=OSError("gone")):
            self.assertEqual(self.fetch()[0], 502)
            self.assertEqual(self.fetch("/api/rows")[0], 502)
        self.docker([])
        self.assertEqual(self.fetch()[0], 200)


class Policy(Server):
    def test_scripts_and_styles_only_from_this_server_and_picos_cdn(self):
        self.docker([])
        _, headers, _ = self.fetch("/")
        policy = headers["Content-Security-Policy"]
        self.assertIn("script-src 'self';", policy)
        self.assertIn("style-src 'self' https://cdn.jsdelivr.net;", policy)
        self.assertIn("connect-src 'self';", policy)
        self.assertIn("img-src data:;", policy)  # Pico's checkbox tick and search icon are data: images
        self.assertNotIn("unsafe-inline", policy)
        self.assertNotIn("nonce", policy)

    def test_checks_page_may_also_fetch_the_sites_it_tests(self):
        with mock.patch.object(diagnostics, "collect", return_value=healthy(containers=[])):
            _, headers, _ = self.fetch("/diagnose")
        self.assertIn("connect-src 'self' https:;", headers["Content-Security-Policy"])


class Api(Server):
    def test_rows_as_json_for_the_page_to_poll(self):
        self.docker([
            container({"caddy": "a.localhost", "caddy.respond": "hi"}, name="/a-1"),
            container({}, name="/db-1"),
        ])
        status, headers, body = self.fetch("/api/rows")
        data = json.loads(body)
        self.assertEqual((status, headers["Content-Type"].split(";")[0]), (200, "application/json"))
        self.assertEqual([(r["kind"], r["state"], r["name"]) for r in data["rows"]], [("site", "ok", "a.localhost"), ("other", "none", "db-1")])
        self.assertIn("1 site from 1 container(s)", data["summary"])
        self.assertIn("1 other container not using Caddy", data["summary"])
        self.assertEqual(headers["Cache-Control"], "no-store")

    def test_hostile_values_are_data_not_markup(self):
        self.docker([container({"caddy": "a.localhost", "caddy.respond": "<script>"}, name="/<img src=x>")])
        row = json.loads(self.fetch("/api/rows")[2])["rows"][0]
        self.assertEqual(row["container"], "<img src=x>")  # the page sets it as text

    def test_head_works(self):
        self.docker([])
        status, _, body = self.fetch("/api/rows", method="HEAD")
        self.assertEqual((status, body), (200, b""))


class Static(Server):
    def test_serves_the_script_and_stylesheet(self):
        for name, kind in (("app.js", "text/javascript"), ("app.css", "text/css")):
            status, headers, body = self.fetch(f"/static/{name}")
            self.assertEqual(status, 200)
            self.assertEqual(headers["Content-Type"].split(";")[0], kind)
            with open(os.path.join(DNS_DIR, "static", name), "rb") as file:
                self.assertEqual(body, file.read())

    def test_nothing_else_can_be_read(self):
        for path in ("/static/web.py", "/static/../web.py", "/static/..%2fweb.py", "/static/", "/static/app.js/../../web.py", "/static/missing.js"):
            self.assertEqual(self.fetch(path)[0], 404, path)


class Pages(Server):
    def test_home_has_the_table_instructions_and_a_checks_button(self):
        stack = {"caddy": {"Name": "/stack-caddy-1", "State": {"Status": "running", "StartedAt": "2026-10-18T10:00:00Z"}, "RestartCount": 0}}
        self.docker([container({"caddy": "a.localhost", "caddy.respond": "hi"})], stack)
        _, _, body = self.fetch("/")
        page = body.decode()
        self.assertIn("<h2>What's running</h2>", page)
        self.assertIn("1 site from 1 container", page)
        self.assertIn("<td>caddy</td>", page)
        self.assertIn("<td>stack-caddy-1</td>", page)
        self.assertNotIn("Restarts", page)
        self.assertIn("Add a site", page)
        self.assertIn("docker compose up -d", page)
        self.assertIn("{{upstreams 3000}}", page)
        self.assertIn('action="/diagnose"', page)
        self.assertIn("just doctor", page)
        self.assertIn('<script src="/static/app.js" defer></script>', page)

    def test_home_without_a_known_stack_just_skips_the_service_rows(self):
        self.docker([])
        page = self.fetch("/")[2].decode()
        self.assertNotIn("<td>caddy</td>", page)
        self.assertIn("Add a site", page)

    def test_other_containers_are_listed_with_a_grey_dash(self):
        self.docker([container({}, name="/db-1")])
        page = self.fetch("/")[2].decode()
        self.assertIn("<td>db-1</td>", page)
        self.assertIn("#8c959f", page)

    def test_diagnose_page(self):
        containers = [
            container({"caddy": "ok.localhost", "caddy.respond": "hi"}),
            container({"caddy": "bare.localhost"}, name="/bare-1"),
            container({"caddy": "http://plain.localhost", "caddy.respond": "x"}, name="/plain-1"),
        ]
        with mock.patch.object(diagnostics, "collect", return_value=healthy(containers=containers)):
            _, _, body = self.fetch("/diagnose")
        page = body.decode()
        self.assertIn("1 thing to look at.", page)
        self.assertIn("bare-1: bare.localhost has no directive", page)
        self.assertIn("Fix:", page)
        self.assertIn("just flush-dns", page)
        self.assertIn('<tr data-url="https://ok.localhost">', page)
        self.assertNotIn('"http://plain.localhost"', page.split("From this browser")[1])

    def test_diagnose_escapes_what_containers_say(self):
        hostile = container({"caddy": "x.localhost"}, name="/<img src=x onerror=alert(1)>")
        with mock.patch.object(diagnostics, "collect", return_value=healthy(containers=[hostile])):
            page = self.fetch("/diagnose")[2].decode()
        self.assertNotIn("<img", page)
        self.assertIn("&lt;img", page)


if __name__ == "__main__":
    unittest.main()
