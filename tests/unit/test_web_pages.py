"""Turning container labels into routes, and routes into HTML."""

import json
import re
import unittest

import web
from diagnostics import ServiceRow
from tests.support import web_container as container


class Site(unittest.TestCase):
    def test_links(self):
        for address, expected in {
            "app.localhost": ("app.localhost", "https://app.localhost"),
            "Ridiculous.com": ("ridiculous.com", "https://ridiculous.com"),
            "https://x.example:8443/path": ("https://x.example:8443", "https://x.example:8443"),
            "http://plain.example": ("http://plain.example", "http://plain.example"),
            "plain.example:80": ("plain.example:80", "http://plain.example:80"),
            "localhost": ("localhost", "https://localhost"),
            "bücher.example": ("xn--bcher-kva.example", "https://xn--bcher-kva.example"),
            "*.wild.dev": ("*.wild.dev", None),
        }.items():
            self.assertEqual(web.site(address), expected, address)

    def test_skipped(self):
        for bad in [
            "", ":8080", "{{env}}", "1.2.3.4", "[::1]", "javascript://x.example", "h2c://x.example",
            "a" * 64 + ".example", "bad_name.example", "x.example\"><script>",
        ]:
            self.assertIsNone(web.site(bad), bad)


class Routes(unittest.TestCase):
    def test_collects_and_maps_reverse_proxy_per_index(self):
        found = web.routes([
            container(
                {
                    "caddy_0": "a.localhost",
                    "caddy_0.reverse_proxy": "{{upstreams 3000}}",
                    "caddy_1": "b.localhost, c.localhost",
                    "caddy_1.reverse_proxy": "{{upstreams 9000}}",
                    "caddy.other": "ignored.localhost",
                    "com.docker.compose.project": "shop",
                    "com.docker.compose.service": "web",
                },
                name="/shop-web-1",
            )
        ])
        self.assertEqual(
            [(r.address, r.target, r.container, r.project) for r in found],
            [
                ("a.localhost", "{{upstreams 3000}}", "shop-web-1", "shop/web"),
                ("b.localhost", "{{upstreams 9000}}", "shop-web-1", "shop/web"),
                ("c.localhost", "{{upstreams 9000}}", "shop-web-1", "shop/web"),
            ],
        )

    def test_flags_containers_outside_the_caddy_network(self):
        found = web.routes([
            container({"caddy": "in.localhost"}),
            container({"caddy": "out.localhost"}, networks=("bridge",)),
        ])
        self.assertEqual(
            {r.address: r.problem for r in found},
            {"in.localhost": "", "out.localhost": "Not on the caddy network, so Caddy can't reach it"},
        )

    def test_unhealthy_and_stopped_containers_are_problems(self):
        unhealthy = {**container({"caddy": "sick.localhost"}), "State": "running", "Status": "Up 2 hours (unhealthy)"}
        restarting = {**container({"caddy": "loop.localhost"}), "State": "restarting", "Status": "Restarting (1) 5s ago"}
        fine = {**container({"caddy": "fine.localhost"}), "State": "running", "Status": "Up 2 hours (healthy)"}
        found = {r.address: r.problem for r in web.routes([unhealthy, restarting, fine])}
        self.assertEqual(found, {"sick.localhost": "Unhealthy", "loop.localhost": "Restarting", "fine.localhost": ""})

    def test_uptime_comes_from_the_inspected_start_time(self):
        started = "2026-10-18T10:00:00Z"
        details = {"abc": {"State": {"StartedAt": started}}}
        found = web.routes([{**container({"caddy": "a.localhost"}), "Id": "abc"}], details, 1_792_317_600 + 7500)
        self.assertEqual(found[0].uptime, "2 h 5 min")
        self.assertEqual(web.routes([{**container({"caddy": "a.localhost"}), "Id": "abc"}])[0].uptime, "")

    def test_no_labels_or_networks(self):
        self.assertEqual(web.routes([{"Names": ["/x"]}, {"Names": ["/y"], "Labels": None}]), [])

    def test_sorted_by_address(self):
        found = web.routes([container({"caddy": "b.localhost a.localhost"})])
        self.assertEqual([r.address for r in found], ["a.localhost", "b.localhost"])


COMPOSE = "com.docker.compose."


def compose(project, service, directory=None):
    labels = {COMPOSE + "project": project, COMPOSE + "service": service}
    if directory:
        labels[COMPOSE + "project.working_dir"] = directory
    return labels


def with_id(box, ident):
    return {**box, "Id": ident}


class BuildRows(unittest.TestCase):
    NOW = 1_792_317_600 + 7500

    def test_a_working_site_is_ok(self):
        rows = web.build_rows([container({"caddy": "a.localhost"})])
        self.assertEqual((rows[0].kind, rows[0].state, rows[0].state_label), ("site", "ok", web.SITE_OK))

    def test_a_site_with_a_problem_is_failing_and_says_why(self):
        rows = web.build_rows([container({"caddy": "a.localhost"}, networks=("bridge",))])
        self.assertEqual((rows[0].state, rows[0].state_label), ("fail", "Not on the caddy network, so Caddy can't reach it"))

    def test_a_container_that_does_not_use_caddy_is_grey_and_not_checked(self):
        labels = {**compose("shop", "postgres", "/Users/me/shop"), "unrelated": "x"}
        rows = web.build_rows([container(labels, name="/shop-postgres-1")])
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual((row.kind, row.state, row.state_label), ("other", "none", web.NOT_CADDY))
        self.assertEqual((row.name, row.container, row.project, row.directory), ("postgres", "shop-postgres-1", "shop/postgres", "/Users/me/shop"))
        self.assertEqual(row.problem, "")

    def test_a_plain_docker_run_container_has_no_folder(self):
        row = web.build_rows([container({}, name="/lonely_euler")])[0]
        self.assertEqual((row.name, row.container, row.project, row.directory), ("lonely_euler", "lonely_euler", "", ""))

    def test_the_directory_of_a_site_comes_from_its_compose_labels(self):
        labels = {"caddy": "a.localhost", **compose("shop", "web", "/Users/me/shop")}
        self.assertEqual(web.build_rows([container(labels)])[0].directory, "/Users/me/shop")

    def test_the_stacks_own_services_get_their_own_state(self):
        services = [
            ServiceRow("caddy", "running", "3 h", 0, "stack-caddy-1", "stack/caddy", "/Users/me/caddy-local", 10800),
            ServiceRow("dns", "not running", "", 0, ""),
        ]
        rows = web.build_rows([container({}, name="/stack-caddy-1")], services=services)
        self.assertEqual([(r.kind, r.name, r.state, r.state_label) for r in rows], [
            ("service", "caddy", "ok", "Running"),
            ("service", "dns", "fail", "Not running"),
        ])
        self.assertEqual((rows[0].uptime, rows[0].uptime_seconds, rows[0].directory), ("3 h", 10800, "/Users/me/caddy-local"))

    def test_a_service_that_serves_a_site_appears_once_as_the_site(self):
        boxes = [container({"caddy": "resolver.localhost"}, name="/stack-dns-web-1")]
        services = [ServiceRow("dns-web", "running", "1 h", 0, "stack-dns-web-1")]
        rows = web.build_rows(boxes, services=services)
        self.assertEqual([(r.kind, r.name) for r in rows], [("site", "resolver.localhost")])

    def test_order_is_sites_then_services_then_others_by_name(self):
        boxes = [
            container({}, name="/zeta-1"),
            container({"caddy": "b.localhost"}, name="/b-1"),
            container({}, name="/alpha-1"),
            container({"caddy": "a.localhost"}, name="/a-1"),
            container({}, name="/stack-caddy-1"),
        ]
        rows = web.build_rows(boxes, services=[ServiceRow("caddy", "running", "", 0, "stack-caddy-1")])
        self.assertEqual([(r.kind, r.name) for r in rows], [
            ("site", "a.localhost"), ("site", "b.localhost"), ("service", "caddy"), ("other", "alpha-1"), ("other", "zeta-1"),
        ])

    def test_uptime_is_filled_in_for_every_kind_of_row(self):
        details = {i: {"State": {"StartedAt": "2026-10-18T10:00:00Z"}} for i in ("s", "o")}
        boxes = [with_id(container({"caddy": "a.localhost"}), "s"), with_id(container({}, name="/other-1"), "o")]
        rows = web.build_rows(boxes, details, now=self.NOW)
        self.assertEqual([(r.uptime, r.uptime_seconds) for r in rows], [("2 h 5 min", 7500)] * 2)
        self.assertEqual(web.build_rows(boxes)[0].uptime_seconds, -1)

    def test_summary_counts(self):
        rows = web.build_rows([container({"caddy": "a.localhost b.localhost"}), container({}, name="/x-1"), container({}, name="/y-1")])
        self.assertEqual(web.summarise(rows, 3), "2 sites from 1 container(s) \u00b7 3 domains pointed at this Mac by DNS \u00b7 2 other containers not using Caddy")
        self.assertEqual(web.summarise(rows[:1]), "1 site from 1 container(s)")

    def test_rows_serialise_to_json(self):
        data = json.loads(json.dumps(web.build_rows([container({"caddy": "a.localhost"})])[0]._asdict()))
        self.assertEqual(
            sorted(data),
            sorted(["kind", "state", "state_label", "name", "link", "container", "project", "target", "problem", "uptime", "uptime_seconds", "directory"]),
        )


class Render(unittest.TestCase):
    GREEN, RED, GREY = "#2da44e", "#d1242f", "#8c959f"

    def page(self, boxes, services=None, summary="s"):
        return web.render(web.build_rows(boxes, services=services), summary)

    def rows(self, page):
        body = page.split("<tbody>")[1].split("</tbody>")[0]
        return re.findall(r"<tr>(.*?)</tr>", body, re.S)

    def test_hostile_labels_names_and_folders_are_escaped(self):
        labels = {
            "caddy": 'ok.localhost x"><script>alert(1)</script>.example',
            "caddy.reverse_proxy": "<img src=x onerror=alert(1)>",
            **compose("<b>p</b>", "web", '/tmp/"><script>alert(3)</script>'),
        }
        page = self.page([container(labels, name="/<script>alert(2)</script>")])
        for bad in ("<script>", "<img", "<b>p", '"><script'):
            self.assertNotIn(bad, page.replace('<script src="/static/app.js" defer></script>', ""))
        self.assertIn("&lt;script&gt;alert(2)&lt;/script&gt;", page)
        self.assertIn('<a href="https://ok.localhost" rel="noreferrer">ok.localhost</a>', page)
        self.assertIn('data-copy="/tmp/&quot;&gt;&lt;script&gt;alert(3)&lt;/script&gt;"', page)

    def test_wildcard_is_not_a_link(self):
        page = self.page([container({"caddy": "*.wild.dev"})])
        self.assertIn("<td>*.wild.dev</td>", page)
        self.assertNotIn("<a ", page)

    def test_empty_state_and_pico(self):
        page = web.render([], "")
        self.assertIn("No containers are running.", page)
        self.assertIn("pico.min.css", page)
        self.assertIn('<main class="container">', page)
        self.assertIn('name="color-scheme"', page)

    def test_one_table_with_the_columns_you_can_sort(self):
        page = self.page([container({"caddy": "a.localhost"})])
        self.assertEqual(page.count("<table"), 1)
        self.assertEqual(re.findall(r'data-sort="(\w+)"', page), ["state", "name", "container", "project", "target", "uptime"])
        for label in ("State", "Site or service", "Container", "Compose service", "Target", "Uptime"):
            self.assertIn(f">{label}</th>", page)
        self.assertNotIn("Restarts", page)
        self.assertIn("<h2>What's running</h2>", page)
        self.assertNotIn("<h2>Status</h2>", page)

    def test_search_box_and_the_toggle_for_other_containers(self):
        page = self.page([container({"caddy": "a.localhost"})])
        self.assertIn('<input type="search" id="filter"', page)
        self.assertIn('<input type="checkbox" id="show-other">', page)

    def test_scripts_and_styles_come_from_this_server_not_inline(self):
        page = self.page([container({"caddy": "a.localhost"})])
        self.assertIn('<script src="/static/app.js" defer></script>', page)
        self.assertIn('href="/static/app.css"', page)
        self.assertNotIn("style=", page)
        self.assertEqual(page.count("<script"), 1)

    def test_icons_template_has_one_icon_per_state(self):
        page = self.page([container({"caddy": "a.localhost"})])
        for kind in ("ok", "fail", "none"):
            self.assertIn(f'<template data-icon="{kind}"><svg', page)

    def test_good_is_green_bad_is_red_and_not_caddy_is_a_grey_dash(self):
        boxes = [
            container({"caddy": "up.localhost"}, name="/up-1"),
            container({"caddy": "down.localhost"}, name="/down-1", networks=("bridge",)),
            container({}, name="/plain-1"),
        ]
        down, up, plain = self.rows(self.page(boxes))
        icon_of = lambda row: row.split("</td>")[0]
        self.assertIn(self.GREEN, icon_of(up))
        self.assertIn(self.RED, icon_of(down))
        self.assertIn(self.GREY, icon_of(plain))
        for colour in (self.GREEN, self.RED):
            self.assertNotIn(colour, icon_of(plain))
        self.assertIn('d="M4.8 8h6.4"', icon_of(plain))  # a dash, not a tick or cross
        self.assertIn("Caddy can&#x27;t reach it", down)

    def test_the_state_cell_has_a_hover_with_the_reason(self):
        page = self.page([container({}, name="/plain-1")])
        self.assertIn('class="tip" data-tip="Not using Caddy, so this tool doesn&#x27;t check it"', page)
        self.assertIn('title="Not using Caddy, so this tool doesn&#x27;t check it"', page)

    def test_the_container_cell_offers_the_folder_to_copy_only_when_known(self):
        with_dir = self.page([container(compose("shop", "web", "/Users/me/shop"), name="/shop-web-1")])
        self.assertIn('class="tip copy" data-tip="/Users/me/shop" data-copy="/Users/me/shop"', with_dir)
        without = self.page([container({}, name="/lonely-1")])
        self.assertNotIn("tip copy", without)
        self.assertIn("<td>lonely-1</td>", without)

    def test_no_empty_code_box_when_there_is_no_target(self):
        self.assertNotIn("<code></code>", self.page([container({"caddy": "a.localhost"})]))
        self.assertIn("<code>x:1</code>", self.page([container({"caddy": "a.localhost", "caddy.reverse_proxy": "x:1"})]))

    def test_uptime_is_shown_per_row(self):
        details = {"x": {"State": {"StartedAt": "2026-10-18T10:00:00Z"}}}
        rows = web.build_rows([with_id(container({"caddy": "a.localhost"}), "x")], details, now=1_792_317_600 + 90)
        self.assertIn('<td class="uptime">1 min</td>', web.render(rows, ""))

    def test_a_service_that_is_down_is_red_and_says_so(self):
        page = self.page([], [ServiceRow("dns", "not running", "", 0, "")])
        self.assertIn(self.RED, page)
        self.assertIn("Not running", page)

    def test_hostile_service_names_are_escaped(self):
        page = self.page([], [ServiceRow("<b>x</b>", "running", "", 0, "<i>y</i>")])
        self.assertNotIn("<b>x", page)
        self.assertNotIn("<i>y", page)


class Icons(unittest.TestCase):
    def test_none_is_grey_with_a_dash(self):
        svg = web.icon("none", "Not used")
        self.assertIn("#8c959f", svg)
        self.assertIn("M4.8 8h6.4", svg)

    def test_label_is_for_screen_readers_and_optionally_a_native_tooltip(self):
        self.assertIn("<title>Running</title>", web.icon("ok", "Running"))
        self.assertNotIn("<title>", web.icon("ok", "Running", titled=False))
        self.assertIn('aria-label="Running"', web.icon("ok", "Running", titled=False))
