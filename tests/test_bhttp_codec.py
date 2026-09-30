"""BHTTP/1 bytes: the header, the payloads, and the reader's skip rule."""
import socket
import unittest

from app.models.bhttp import END_STREAM, FrameType, Method
from app.network.bhttp_codec import (
    FrameReader, FramingError, MalformedFrame, PayloadReader, decode_header_block,
    decode_request, decode_response, encode_frame, encode_header_block, encode_request,
    encode_response, pack_header, unpack_header,
)


class HeaderTest(unittest.TestCase):
    def test_layout_is_sync_type_flags_stream_length(self):
        header = pack_header(FrameType.DATA, END_STREAM, 0x0102, 0x030405)
        self.assertEqual(header.hex(" "), "bf 03 01 01 02 03 04 05")
        self.assertEqual(unpack_header(header), (3, 1, 0x0102, 0x030405))

    def test_bad_sync_is_a_framing_error(self):
        with self.assertRaises(FramingError):
            unpack_header(b"GET / HT")

    def test_fields_that_do_not_fit_are_refused(self):
        with self.assertRaises(ValueError):
            pack_header(1, 0, 0x10000, 0)
        with self.assertRaises(ValueError):
            pack_header(1, 0, 1, 0x1000000)


class PayloadTest(unittest.TestCase):
    def test_request_round_trip(self):
        headers = [("host", "localhost:9000"), ("x-custom", "yes")]
        payload = encode_request(Method.GET, "/index.html", headers)
        self.assertEqual(decode_request(payload), (Method.GET, "/index.html", headers))

    def test_response_round_trip(self):
        payload = encode_response(404, [("content-length", "0")])
        self.assertEqual(decode_response(payload), (404, [("content-length", "0")]))

    def test_table_names_cost_one_octet_literals_are_length_prefixed(self):
        self.assertEqual(encode_header_block([("host", "a")]).hex(" "), "01 00 01 61")
        self.assertEqual(encode_header_block([("x-a", "b")]).hex(" "), "00 00 03 78 2d 61 00 01 62")

    def test_unassigned_table_index_is_skipped(self):
        block = bytes([0x63]) + b"\x00\x02zz" + encode_header_block([("host", "h")])
        self.assertEqual(decode_header_block(PayloadReader(block)), [("host", "h")])

    def test_truncated_payloads_are_malformed_not_crashes(self):
        good = encode_request(Method.GET, "/index.html", [("host", "h")])
        headers_start = 1 + 2 + len("/index.html")   # cutting here leaves a valid, header-less request
        for cut in range(len(good)):
            if cut == headers_start:
                continue
            with self.subTest(cut=cut), self.assertRaises(MalformedFrame):
                decode_request(good[:cut])

    def test_path_must_be_absolute(self):
        with self.assertRaises(MalformedFrame):
            decode_request(encode_request(Method.GET, "index.html", []))


class ReaderTest(unittest.TestCase):
    def setUp(self):
        self.a, self.b = socket.socketpair()
        self.reader = FrameReader(self.b)

    def tearDown(self):
        self.a.close()
        self.b.close()

    def test_two_frames_in_one_write_come_out_as_two(self):
        self.a.sendall(encode_frame(FrameType.DATA, 0, 1, b"first") +
                       encode_frame(FrameType.DATA, END_STREAM, 1, b"second"))
        self.assertEqual(self.reader.read_frame(1, 1).payload, b"first")
        second = self.reader.read_frame(1, 1)
        self.assertEqual((second.payload, second.end_stream), (b"second", True))

    def test_unknown_frame_type_is_skipped_cleanly(self):
        self.a.sendall(encode_frame(0x7E, 0xFF, 9, b"\xbf" * 300) +
                       encode_frame(FrameType.DATA, 0, 1, b"after"))
        skipped = self.reader.read_frame(1, 1)
        self.assertEqual((skipped.skipped, skipped.length, skipped.type_name), (True, 300, "UNKNOWN-0x7E"))
        self.assertEqual(self.reader.read_frame(1, 1).payload, b"after")

    def test_oversized_control_frame_is_skipped(self):
        reader = FrameReader(self.b, max_control_payload=10)
        self.a.sendall(encode_frame(FrameType.REQUEST, 0, 1, b"x" * 11) +
                       encode_frame(FrameType.DATA, 0, 1, b"ok"))
        self.assertTrue(reader.read_frame(1, 1).skipped)
        self.assertEqual(reader.read_frame(1, 1).payload, b"ok")

    def test_clean_eof_between_frames_is_none(self):
        self.a.close()
        self.assertIsNone(self.reader.read_frame(1, 1))

    def test_eof_inside_a_frame_is_a_framing_error(self):
        self.a.sendall(encode_frame(FrameType.DATA, 0, 1, b"12345")[:-2])
        self.a.close()
        with self.assertRaises(FramingError):
            self.reader.read_frame(1, 1)


if __name__ == "__main__":
    unittest.main()
