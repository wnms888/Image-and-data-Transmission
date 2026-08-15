import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from protocol import (
    PACKET_IMAGE_RGB565,
    PACKET_TEXT,
    PIXEL_FLAG_RGB565_MSB_FIRST,
    PIXEL_LAYOUT_RGB565_MSB,
    RawPrintfParser,
    StreamParser,
    encode_packet,
    fit_size,
    parse_printf_line,
    parse_text_payload,
    resize_rgb888_nearest,
    rgb565_to_rgb888,
    rgb565_to_ppm,
    rgb888_to_png,
)


class StreamParserTests(unittest.TestCase):
    def test_fragmented_image_then_text(self):
        image = bytes((0x00, 0xF8, 0xE0, 0x07, 0x1F, 0x00, 0xFF, 0xFF))
        stream = b"noise" + encode_packet(
            PACKET_IMAGE_RGB565, image, sequence=7, width=2, height=2, pixel_format=2
        ) + encode_packet(PACKET_TEXT, b"[warning]speed:666\\n", sequence=8)
        parser = StreamParser()
        packets = []
        for byte in stream:
            packets.extend(parser.feed(bytes([byte])))
        self.assertEqual([packet.packet_type for packet in packets], [PACKET_IMAGE_RGB565, PACKET_TEXT])
        self.assertEqual(packets[0].width, 2)
        self.assertEqual(packets[0].payload, image)
        self.assertEqual(parse_text_payload(packets[1].payload)[0].level, "warning")
        self.assertGreaterEqual(parser.dropped_bytes, 5)

    def test_crc_and_resynchronization(self):
        broken = bytearray(encode_packet(PACKET_TEXT, b"[info] bad\\n"))
        broken[16] ^= 0xFF
        good = encode_packet(PACKET_TEXT, b"[error]state:1,2\\n", sequence=99, include_payload_crc=True)
        parser = StreamParser()
        packets = parser.feed(bytes(broken) + good)
        self.assertEqual(len(packets), 1)
        self.assertEqual(packets[0].sequence, 99)
        self.assertGreater(parser.header_crc_errors, 0)

    def test_optional_payload_crc_rejects_corruption(self):
        frame = bytearray(encode_packet(PACKET_TEXT, b"[info] intact\n", include_payload_crc=True))
        frame[-2] ^= 0x01
        parser = StreamParser()
        self.assertEqual(parser.feed(frame), [])
        self.assertEqual(parser.payload_crc_errors, 1)


class PrintfParserTests(unittest.TestCase):
    def test_documented_formats_and_string_debug(self):
        speed = parse_printf_line("[info]speed:666 \n")
        self.assertEqual((speed.level, speed.description, speed.values), ("info", "speed", (666.0,)))
        channels = parse_printf_line("channel_data:12,13,14,15\n")
        self.assertEqual(channels.description, "channel_data")
        self.assertEqual(channels.values, (12.0, 13.0, 14.0, 15.0))
        no_description = parse_printf_line("12.389,13.613,14.674,15,612,-11.936\n")
        self.assertEqual(no_description.description, "无描述")
        self.assertEqual(len(no_description.values), 6)
        text_only = parse_printf_line("[info] Base_StartWaitingIMU\n")
        self.assertEqual((text_only.level, text_only.description, text_only.values), ("info", "Base_StartWaitingIMU", ()))

    def test_raw_serial_lines(self):
        parser = RawPrintfParser()
        self.assertEqual(parser.feed(b"[warning]imu:"), [])
        messages = parser.feed(b"1,2,3\n")
        self.assertEqual(messages[0].level, "warning")
        self.assertEqual(messages[0].values, (1.0, 2.0, 3.0))

    def test_rgb565_to_ppm(self):
        ppm = rgb565_to_ppm(bytes((0x00, 0xF8)), 1, 1)
        self.assertEqual(ppm, b"P6\n1 1\n255\n" + bytes((0xFF, 0x00, 0x00)))

    def test_scc8660_high_byte_first_and_aspect_scaling(self):
        # The camera's red word is sent as F8 00, not the host-native 00 F8.
        self.assertEqual(
            rgb565_to_rgb888(
                bytes((0xF8, 0x00)),
                1,
                1,
                layout=PIXEL_LAYOUT_RGB565_MSB,
                packet_flags=PIXEL_FLAG_RGB565_MSB_FIRST,
            ),
            bytes((0xFF, 0x00, 0x00)),
        )
        self.assertEqual(fit_size(188, 120, 600, 600), (600, 383))
        scaled = resize_rgb888_nearest(bytes((255, 0, 0, 0, 0, 255)), 2, 1, 4, 2)
        expected_row = bytes((255, 0, 0)) * 2 + bytes((0, 0, 255)) * 2
        self.assertEqual(scaled, expected_row * 2)
        self.assertTrue(rgb888_to_png(bytes((255, 0, 0)), 1, 1).startswith(b"\x89PNG\r\n\x1a\n"))


if __name__ == "__main__":
    unittest.main()
