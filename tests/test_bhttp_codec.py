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


class SpecVectorTest(unittest.TestCase):
    """The bytes printed in spec/BHTTP-1.md section 6 -- if these fail, the spec is wrong."""

    V1 = ("bf0101000100002801000b2f696e6465782e68746d6c"
          "01000e6c6f63616c686f73743a39303030000003646e74000131")
    V2_RESPONSE = "bf0200000100001300c806000a746578742f706c61696e07000132"
    V2_DATA = "bf030100010000026869"
    V3 = "bff7000000000003616263"

    def test_v1_request(self):
        headers = [("host", "localhost:9000"), ("dnt", "1")]
        wire = encode_frame(FrameType.REQUEST, END_STREAM, 1, encode_request(Method.GET, "/index.html", headers))
        self.assertEqual(wire.hex(), self.V1)
        self.assertEqual(decode_request(wire[8:]), (Method.GET, "/index.html", headers))

    def test_v2_response_and_data(self):
        headers = [("content-type", "text/plain"), ("content-length", "2")]
        self.assertEqual(encode_frame(FrameType.RESPONSE, 0, 1, encode_response(200, headers)).hex(),
                         self.V2_RESPONSE)
        self.assertEqual(encode_frame(FrameType.DATA, END_STREAM, 1, b"hi").hex(), self.V2_DATA)

    def test_v3_unknown_frame_is_skipped(self):
        a, b = socket.socketpair()
        with a, b:
            a.sendall(bytes.fromhex(self.V3) + bytes.fromhex(self.V2_DATA))
            reader = FrameReader(b)
            self.assertTrue(reader.read_frame(1, 1).skipped)
            self.assertEqual(reader.read_frame(1, 1).payload, b"hi")


if __name__ == "__main__":
    unittest.main()
