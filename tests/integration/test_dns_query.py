"""The DNS query helper against a UDP server on localhost."""

import socket
import threading
import unittest

import diagnostics as d


class DnsQuery(unittest.TestCase):
    def serve(self, build_reply):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.bind(("127.0.0.1", 0))
        self.addCleanup(sock.close)

        def loop():
            data, addr = sock.recvfrom(512)
            sock.sendto(build_reply(data), addr)

        threading.Thread(target=loop, daemon=True).start()
        return sock.getsockname()[1]

    def test_reads_an_answer_using_a_compressed_name(self):
        def reply(query):
            question = query[12:]
            header = query[:2] + b"\x81\x80\x00\x01\x00\x01\x00\x00\x00\x00"
            answer = b"\xc0\x0c\x00\x01\x00\x01\x00\x00\x00\x3c\x00\x04\x7f\x00\x00\x01"
            return header + question + answer

        port = self.serve(reply)
        self.assertEqual(d.query_a("127.0.0.1", port, "doctor.test"), ["127.0.0.1"])

    def test_error_code_is_an_error(self):
        port = self.serve(lambda q: q[:2] + b"\x81\x85" + q[4:])
        with self.assertRaises(OSError):
            d.query_a("127.0.0.1", port, "doctor.test")

    def test_silence_times_out(self):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.bind(("127.0.0.1", 0))
        self.addCleanup(sock.close)
        with self.assertRaises(OSError):
            d.query_a("127.0.0.1", sock.getsockname()[1], "doctor.test", timeout=0.2)
