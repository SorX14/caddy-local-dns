"""The checks that say what is wrong with the stack, fed made-up snapshots."""

import json
import struct
import unittest
from unittest import mock

import diagnostics as d
from tests.support import healthy, running, titles
from tests.support import stack_container as container


class Helpers(unittest.TestCase):
    def test_humanize(self):
        self.assertEqual(d.humanize(5), "5 s")
        self.assertEqual(d.humanize(125), "2 min")
        self.assertEqual(d.humanize(3 * 3600 + 7 * 60), "3 h 7 min")
        self.assertEqual(d.humanize(86400 * 2 + 5 * 3600), "2 days 5 h")
        self.assertEqual(d.humanize(86400 + 3600), "1 day 1 h")
        self.assertEqual(d.humanize(-4), "0 s")

    def test_started_at(self):
        self.assertEqual(d.started_at(running("2026-10-18T10:00:00.123456789Z")), 1_792_317_600)
        self.assertIsNone(d.started_at(running("0001-01-01T00:00:00Z")))
        self.assertIsNone(d.started_at({}))

    def test_demux_logs_framed_and_tty(self):
        framed = b"".join(bytes([1, 0, 0, 0]) + struct.pack(">I", len(p)) + p for p in (b"one\n", b"two\n"))
        self.assertEqual(d.demux_logs(framed), "one\ntwo\n")
        self.assertEqual(d.demux_logs(b'{"level":"info"}\n'), '{"level":"info"}\n')
        self.assertEqual(d.demux_logs(b""), "")

    def test_upstream_ports(self):
        labels = {
            "caddy_0": "a.localhost", "caddy_0.reverse_proxy": "{{upstreams 3000}}",
            "caddy_1": "b.localhost", "caddy_1.reverse_proxy": "{{ upstreams https 8443 }}",
            "caddy_2": "c.localhost", "caddy_2.reverse_proxy": "{{upstreams}}",
            "caddy_3": "d.localhost", "caddy_3.reverse_proxy": "other:9000",
        }
        self.assertEqual(d.upstream_ports({"Labels": labels}), {3000, 8443})


class Checks(unittest.TestCase):
    def test_a_healthy_stack_has_no_problems(self):
        checks = d.run(healthy())
        self.assertEqual([c for c in checks if c.status in ("fail", "warn")], [])
        self.assertIn("1 site in 1 container look right", titles(checks))

    def test_site_with_no_directive_fails_and_says_which(self):
        snap = healthy(containers=[container("bare-1", {"caddy": "foo.localhost bar.localhost"})])
        checks = d.check_labels(snap)
        self.assertEqual(titles(checks, "fail"), ["bare-1: foo.localhost bar.localhost has no directive"])
        self.assertIn("caddy.reverse_proxy", checks[0].fix)

    def test_directive_must_belong_to_the_same_site_label(self):
        labels = {"caddy_0": "a.localhost", "caddy_0.respond": "hi", "caddy_1": "b.localhost"}
        checks = d.check_labels(healthy(containers=[container("x-1", labels)]))
        self.assertEqual(titles(checks, "fail"), ["x-1: b.localhost has no directive"])

    def test_off_network_and_duplicates_warn(self):
        a = container("a-1", {"caddy": "same.localhost", "caddy.respond": "a"}, networks=("bridge",))
        b = container("b-1", {"caddy": "same.localhost", "caddy.respond": "b"})
        checks = d.check_labels(healthy(containers=[a, b]))
        self.assertEqual(
            sorted(titles(checks, "warn")),
            ["a-1 isn't on the caddy network", "same.localhost is claimed by 2 containers"],
        )

    def test_no_sites_is_just_information(self):
        checks = d.check_labels(healthy(containers=[container("plain-1", {})]))
        self.assertEqual([c.status for c in checks], ["info"])

    def test_dead_upstream_port_warns(self):
        checks = d.check_upstreams(healthy(probes={("shop-web-1", 3000): False}))
        self.assertEqual(titles(checks, "warn"), ["Nothing answers on port 3000 in shop-web-1"])
        self.assertIn("0.0.0.0", checks[0].fix)
        self.assertIn("{{upstreams 3000}}", checks[0].fix)

    def test_upstream_probe_missing_is_not_a_failure(self):
        self.assertEqual(d.check_upstreams(healthy(probes={})), [])

    def test_services(self):
        stack = {name: running() for name in d.EXPECTED_SERVICES}
        stack["dns"] = running(status="exited")
        stack["dns-sync"] = running(restarts=5)
        del stack["dns-web"]
        checks = d.check_services(healthy(stack=stack))
        self.assertEqual(titles(checks, "fail"), ["dns is exited", "dns-web is not running"])
        self.assertEqual(titles(checks, "warn"), ["dns-sync has restarted 5 times"])
        self.assertIn("caddy is running", titles(checks, "ok"))

    def test_unknown_stack_is_skipped_not_failed(self):
        self.assertEqual([c.status for c in d.check_services(healthy(stack={}))], ["info"])

    def test_caddyfile_mount(self):
        self.assertEqual(d.check_caddyfile(healthy())[0].status, "ok")
        stack = {"caddy": running()}
        self.assertEqual(d.check_caddyfile(healthy(stack=stack))[0].status, "fail")
        self.assertEqual(d.check_caddyfile(healthy(stack={})), [])

    def test_caddy_errors_are_summarised_with_a_hint(self):
        logs = "\n".join([
            json.dumps({"level": "info", "msg": "fine"}),
            json.dumps({"level": "info", "logger": "docker-proxy", "logs": "[ERROR]  Removing invalid block: x\nmore"}),
            json.dumps({"level": "error", "msg": "subject does not qualify for certificate: 'app.test.'"}),
            json.dumps({"level": "error", "msg": "subject does not qualify for certificate: 'app.test.'"}),
            "not json",
        ])
        checks = d.check_caddy_errors(healthy(caddy_logs=logs))
        self.assertEqual(checks[0].status, "warn")
        self.assertIn("2 different errors", checks[0].title)
        self.assertIn("app.test.", checks[0].detail)
        self.assertIn("typo", checks[0].fix)

    def test_quiet_or_missing_caddy_logs(self):
        self.assertEqual(d.check_caddy_errors(healthy(caddy_logs=json.dumps({"level": "info"})))[0].status, "ok")
        self.assertEqual(d.check_caddy_errors(healthy(caddy_logs=None)), [])

    def test_dns(self):
        labels = {"caddy": "ridiculous.com", "caddy.respond": "hi"}
        containers = [container("r-1", labels)]
        bad = d.check_dns(healthy(containers=containers, dns_answer="timed out", resolver_files={"old.example"}))
        self.assertEqual(
            titles(bad),
            ["The dns service isn't answering", "No DNS entry yet for ridiculous.com", "Leftover DNS entries: old.example"],
        )
        good = d.check_dns(healthy(containers=containers, resolver_files={"ridiculous.com"}))
        self.assertEqual(titles(good), ["The dns service is answering", "1 domain points at this Mac"])
        self.assertEqual(d.check_dns(healthy(resolver_files=None))[1].status, "warn")

    def test_localhost_names_need_no_dns_entry(self):
        checks = d.check_dns(healthy())
        self.assertEqual(titles(checks)[1], "No domains need DNS entries")

    def test_service_rows(self):
        stack = {"caddy": running("2026-10-18T10:00:00Z", restarts=1), "dns": running(status="exited")}
        rows = d.service_rows(stack, 1_792_317_600 + 7500)
        self.assertEqual(rows[0][:5], ("caddy", "running", "2 h 5 min", 1, ""))
        self.assertEqual(rows[0].seconds, 7500)
        self.assertEqual(rows[1][:5], ("dns", "exited", "", 0, ""))
        self.assertEqual(rows[1].seconds, -1)
        self.assertEqual(rows[2][:5], ("dns-sync", "not running", "", 0, ""))

    def test_service_rows_carry_the_container_project_and_folder(self):
        info = {
            **running(),
            "Name": "/stack-caddy-1",
            "Config": {"Labels": {
                "com.docker.compose.project": "stack",
                "com.docker.compose.service": "caddy",
                "com.docker.compose.project.working_dir": "/Users/me/projects/caddy-local",
            }},
        }
        row = d.service_rows({"caddy": info}, 1_792_317_600)[0]
        self.assertEqual(
            (row.container, row.project, row.directory), ("stack-caddy-1", "stack/caddy", "/Users/me/projects/caddy-local")
        )

    def test_compose_project(self):
        self.assertEqual(d.compose_project({"com.docker.compose.project": "p", "com.docker.compose.service": "s"}), "p/s")
        self.assertEqual(d.compose_project({"com.docker.compose.project": "p"}), "p")
        self.assertEqual(d.compose_project(None), "")


class Script(unittest.TestCase):
    def test_prints_results_and_exit_code(self):
        bad = healthy(containers=[container("bare-1", {"caddy": "foo.localhost"})])
        with mock.patch.object(d, "collect", return_value=bad), mock.patch("builtins.print") as out:
            self.assertEqual(d.main(), 1)
        text = "\n".join(str(call.args[0]) for call in out.call_args_list)
        self.assertIn("[FAIL] bare-1: foo.localhost has no directive", text)
        self.assertIn("just flush-dns", text)
        with mock.patch.object(d, "collect", return_value=healthy()), mock.patch("builtins.print"):
            self.assertEqual(d.main(), 0)


if __name__ == "__main__":
    unittest.main()
