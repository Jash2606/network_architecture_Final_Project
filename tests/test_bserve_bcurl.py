"""bserve and bcurl over real sockets, including a stranger's raw bytes."""
import io
import logging
import os
import socket
import subprocess
import sys
import tempfile
import time
import unittest

from app.config import BServeConfig
from app.controllers.bcurl_controller import CLIENT_ERROR, OK, USAGE, BcurlController
from app.controllers.file_controller import FileController
from app.models.bhttp import END_STREAM, FrameType, Method
from app.models.file_store import FileStore
from app.network.bhttp_codec import (
    FrameReader, decode_goaway, decode_response, encode_frame, encode_request,
)
from app.network.bhttp_server import BServe
from app.views.bhttp_view import BHttpView

logging.disable(logging.CRITICAL)
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BIG = bytes(range(256)) * 400          # 100 KiB: several 16 KiB DATA frames


class BServeCase(unittest.TestCase):
    grease = False

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        with open(os.path.join(cls.tmp.name, "index.html"), "wb") as f:
            f.write(b"<h1>hi</h1>\n")
        with open(os.path.join(cls.tmp.name, "big.bin"), "wb") as f:
            f.write(BIG)
        open(os.path.join(cls.tmp.name, "empty.txt"), "wb").close()
        config = BServeConfig(root=cls.tmp.name, host="127.0.0.1", port=0,
                              idle_timeout=2.0, frame_timeout=1.0, grease=cls.grease)
        view = BHttpView(data_frame_size=config.data_frame_size, grease=cls.grease)
        cls.server = BServe(FileController(FileStore(cls.tmp.name), view), config, view)
        cls.server.start_in_thread()
        cls.base = f"127.0.0.1:{cls.server.port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        time.sleep(0.6)
        cls.tmp.cleanup()

    def bcurl(self, *urls, **options):
        out, err = io.BytesIO(), io.StringIO()
        code = BcurlController(out=out, err=err).run([self.base + u for u in urls], **options)
        return code, out.getvalue(), err.getvalue()


class BcurlTest(BServeCase):
    def test_get(self):
        self.assertEqual(self.bcurl("/index.html"), (OK, b"<h1>hi</h1>\n", ""))

    def test_directory_maps_to_index(self):
        self.assertEqual(self.bcurl("/")[1], b"<h1>hi</h1>\n")

    def test_large_file_spans_several_data_frames(self):
        code, body, _ = self.bcurl("/big.bin")
        self.assertEqual((code, body), (OK, BIG))

    def test_empty_file(self):
        self.assertEqual(self.bcurl("/empty.txt")[:2], (OK, b""))

    def test_error_statuses_exit_non_zero(self):
        self.assertEqual(self.bcurl("/missing")[0], CLIENT_ERROR)
        self.assertEqual(self.bcurl("/../etc/passwd")[0], CLIENT_ERROR)
        self.assertEqual(self.bcurl("/index.html", method=Method.POST)[0], CLIENT_ERROR)

    def test_head_has_no_body(self):
        self.assertEqual(self.bcurl("/big.bin", method=Method.HEAD)[:2], (OK, b""))

    def test_many_requests_one_connection(self):
        before = self.server.tcp.accepted
        code, body, _ = self.bcurl("/index.html", "/big.bin", "/index.html", pipeline=True, grease=True)
        self.assertEqual((code, body), (OK, b"<h1>hi</h1>\n" + BIG + b"<h1>hi</h1>\n"))
        self.assertEqual(self.server.tcp.accepted - before, 1)

    def test_refuses_to_open_a_second_connection(self):
        out, err = io.BytesIO(), io.StringIO()
        code = BcurlController(out=out, err=err).run([self.base + "/a", "localhost:1/b"])
        self.assertEqual(code, USAGE)

    def test_command_line(self):
        run = subprocess.run([sys.executable, os.path.join(ROOT, "bcurl"), "-v", self.base + "/index.html"],
                             capture_output=True, timeout=30)
        self.assertEqual((run.returncode, run.stdout), (0, b"<h1>hi</h1>\n"))
        self.assertIn(b"00000000  bf 01 01 00 01", run.stderr)       # -v hexdumps the frames
        run = subprocess.run([sys.executable, os.path.join(ROOT, "bcurl"), self.base + "/nope"],
                             capture_output=True, timeout=30)
        self.assertEqual(run.returncode, CLIENT_ERROR)


class GreasyServerTest(BServeCase):
    grease = True           # the server sends an unknown frame before every response

    def test_client_skips_frames_it_does_not_know(self):
        self.assertEqual(self.bcurl("/index.html", "/index.html")[:2], (OK, b"<h1>hi</h1>\n" * 2))


class StrangerTest(BServeCase):
    """Raw bytes on a raw socket: what a partner's (possibly buggy) client might send."""

    def setUp(self):
        host, port = self.base.split(":")
        self.sock = socket.create_connection((host, int(port)), timeout=5)
        self.reader = FrameReader(self.sock)

    def tearDown(self):
        self.sock.close()

    def request(self, path="/index.html", stream=1, method=Method.GET):
        return encode_frame(FrameType.REQUEST, END_STREAM, stream, encode_request(method, path, []))

    def read_status(self):
        while True:
            frame = self.reader.read_frame(5, 5)
            if frame.type == FrameType.RESPONSE:
                status = decode_response(frame.payload)[0]
                while not frame.end_stream:
                    frame = self.reader.read_frame(5, 5)
                return status

    def test_malformed_payload_gets_400_and_the_connection_stays_open(self):
        broken = encode_frame(FrameType.REQUEST, END_STREAM, 1, b"\x01\x00\x09/ind")  # path cut short
        self.sock.sendall(broken + self.request(stream=2))
        self.assertEqual(self.read_status(), 400)
        self.assertEqual(self.read_status(), 200)

    def test_unknown_frame_type_is_skipped(self):
        self.sock.sendall(encode_frame(0x42, 0, 1, b"\xbf" * 64) + self.request())
        self.assertEqual(self.read_status(), 200)

    def test_stream_zero_is_not_a_request(self):
        self.sock.sendall(self.request(stream=0) + self.request(stream=1))
        self.assertEqual(self.read_status(), 400)
        self.assertEqual(self.read_status(), 200)

    def test_text_http_gets_goaway_400_and_close(self):
        self.sock.sendall(b"GET / HTTP/1.1\r\nHost: x\r\n\r\n")
        frame = self.reader.read_frame(5, 5)
        self.assertEqual(frame.type, FrameType.GOAWAY)
        self.assertEqual(decode_goaway(frame.payload)[1], 400)
        self.assertIsNone(self.reader.read_frame(5, 5))

    def test_idle_connection_gets_goaway_408(self):
        self.sock.sendall(self.request())
        self.assertEqual(self.read_status(), 200)
        frame = self.reader.read_frame(5, 5)             # arrives after idle_timeout
        self.assertEqual(decode_goaway(frame.payload)[:2], (1, 408))


if __name__ == "__main__":
    unittest.main()
