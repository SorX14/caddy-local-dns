"""Which site addresses become DNS names to claim."""

import unittest

import sync_resolvers as sr


class Hostnames(unittest.TestCase):
    def names(self, **labels):
        return sr.hostnames([labels])

    def test_plain_and_multiple_addresses(self):
        self.assertEqual(self.names(caddy="a.example b.example"), {"a.example", "b.example"})

    def test_comma_and_newline_separated(self):
        self.assertEqual(
            self.names(caddy="one.example, two.example\nthree.example"),
            {"one.example", "two.example", "three.example"},
        )

    def test_indexed_keys(self):
        self.assertEqual(self.names(caddy_0="a.example", caddy_12="b.example"), {"a.example", "b.example"})

    def test_other_labels_ignored(self):
        self.assertEqual(self.names(**{"caddy.reverse_proxy": "x.example", "caddyx": "y.example"}), set())

    def test_scheme_port_path_case_wildcard(self):
        self.assertEqual(
            self.names(caddy="https://Up.Example:8443/x *.wild.dev http://p.example:80"),
            {"up.example", "wild.dev", "p.example"},
        )

    def test_internationalised_names_become_punycode(self):
        self.assertEqual(self.names(caddy="bücher.example"), {"xn--bcher-kva.example"})
        self.assertEqual(self.names(caddy="xn--bcher-kva.example"), {"xn--bcher-kva.example"})

    def test_rejected(self):
        for bad in [
            "com", "localhost", "app.localhost", "a.b.localhost", "1.2.3.4", "[::1]",
            ":8080", "{{env}}", "-a.example", "a-.example", "a..example", "under_score.example",
            "a" * 64 + ".example", ".".join(["a" * 60] * 5), "../x.example", "x/../../etc",
            "K.example\u0000", "a‮.example",
        ]:
            self.assertEqual(self.names(caddy=bad), set(), bad)

    def test_denied_namespaces(self):
        for bad in [
            "co.uk", "*.co.uk", "github.io", "in-addr.arpa", "x.ip6.arpa", "home.arpa",
            "printer.local", "arpa",
        ]:
            self.assertEqual(self.names(caddy=bad), set(), bad)
        self.assertEqual(self.names(caddy="app.github.io bbc.co.uk"), {"app.github.io", "bbc.co.uk"})

    def test_longest_valid_label(self):
        name = "a" * 63 + ".example"
        self.assertEqual(self.names(caddy=name), {name})

    def test_no_labels(self):
        self.assertEqual(sr.hostnames([None, {}]), set())
