"""IMGT v1 packet and Printf text parsing.

This module deliberately has no GUI or I/O dependencies so the same parser can
be tested with recorded TCP/serial byte streams.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import math
import re
import struct
import zlib

MAGIC = b"\xAA\x55"
VERSION = 1
PACKET_IMAGE_RGB565 = 1
PACKET_TEXT = 2
RGB565_FORMAT = 2
HEADER_SIZE = 22
MAX_PAYLOAD_SIZE = 4 * 1024 * 1024
MAX_TEXT_PAYLOAD_SIZE = 64 * 1024
PIXEL_FLAG_RGB565_MSB_FIRST = 0x01
PACKET_FLAG_PAYLOAD_CRC_PRESENT = 0x02
# Keep in lockstep with WIFI_IMAGE_TRANSFER_ENABLE_PAYLOAD_CRC.  Only packets
# that satisfy this requirement are returned to the GUI.
PAYLOAD_CRC_REQUIRED = True

PIXEL_LAYOUT_AUTO = "auto"
PIXEL_LAYOUT_RGB565_MSB = "rgb565_msb"
PIXEL_LAYOUT_RGB565_LSB = "rgb565_lsb"
PIXEL_LAYOUT_BGR565_MSB = "bgr565_msb"
PIXEL_LAYOUT_BGR565_LSB = "bgr565_lsb"
_HEADER = struct.Struct("<2sBBIHHBBHHI")
_CLASS = re.compile(r"^\s*\[(error|warning|info)\]\s*(.*)$", re.IGNORECASE)


@dataclass(frozen=True)
class Packet:
    """A decoded IMGT frame."""

    packet_type: int
    payload: bytes
    sequence: int
    width: int = 0
    height: int = 0
    pixel_format: int = 0
    flags: int = 0


@dataclass(frozen=True)
class DebugMessage:
    """One parsed line following the documented Printf protocol."""

    level: str
    description: str
    values: tuple[float, ...]
    data_text: str
    received_at: datetime
    raw_text: str


def crc16_ccitt(data: bytes | bytearray) -> int:
    """CRC-16/CCITT-FALSE used by the embedded packet header."""

    crc = 0xFFFF
    for value in data:
        crc ^= value << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) & 0xFFFF if crc & 0x8000 else (crc << 1) & 0xFFFF
    return crc


def encode_packet(
    packet_type: int,
    payload: bytes,
    *,
    sequence: int = 0,
    width: int = 0,
    height: int = 0,
    pixel_format: int = 0,
    flags: int = 0,
    include_payload_crc: bool = True,
) -> bytes:
    """Build a packet for tests, replay tooling, or compatible senders."""

    if include_payload_crc:
        flags |= PACKET_FLAG_PAYLOAD_CRC_PRESENT
    prefix = struct.pack(
        "<2sBBIHHBBH",
        MAGIC,
        VERSION,
        packet_type,
        len(payload),
        width,
        height,
        pixel_format,
        flags & 0xFF,
        sequence & 0xFFFF,
    )
    header_crc = crc16_ccitt(prefix)
    payload_crc = zlib.crc32(payload) & 0xFFFFFFFF if include_payload_crc else 0
    return prefix + struct.pack("<HI", header_crc, payload_crc) + payload


class StreamParser:
    """Incremental binary parser that returns only structurally valid packets.

    Header-CRC failures, malformed headers, missing required payload CRCs, and
    payload-CRC mismatches are counted and discarded before the caller can
    render an image, append a log entry, or feed the oscilloscope.
    """

    def __init__(self, require_payload_crc: bool = PAYLOAD_CRC_REQUIRED) -> None:
        self._buffer = bytearray()
        self.require_payload_crc = require_payload_crc
        self.dropped_bytes = 0
        self.header_crc_errors = 0
        self.payload_crc_errors = 0
        self.missing_payload_crc_errors = 0
        self.invalid_packets = 0

    def reset(self) -> None:
        self._buffer.clear()
        self.dropped_bytes = 0
        self.header_crc_errors = 0
        self.payload_crc_errors = 0
        self.missing_payload_crc_errors = 0
        self.invalid_packets = 0

    def feed(self, incoming: bytes) -> list[Packet]:
        """Accept any-sized fragment from TCP/serial and return full packets."""

        if not incoming:
            return []
        self._buffer.extend(incoming)
        packets: list[Packet] = []

        while True:
            marker_at = self._buffer.find(MAGIC)
            if marker_at < 0:
                # Keep a possible first byte of the two-byte magic for the next
                # serial/TCP read; discard everything else as unrelated noise.
                keep = 1 if self._buffer and self._buffer[-1] == MAGIC[0] else 0
                self.dropped_bytes += len(self._buffer) - keep
                if keep:
                    del self._buffer[:-1]
                else:
                    self._buffer.clear()
                return packets
            if marker_at:
                self.dropped_bytes += marker_at
                del self._buffer[:marker_at]

            if len(self._buffer) < HEADER_SIZE:
                return packets

            (
                _magic,
                version,
                packet_type,
                payload_length,
                width,
                height,
                pixel_format,
                flags,
                sequence,
                header_crc,
                payload_crc,
            ) = _HEADER.unpack_from(self._buffer)

            if crc16_ccitt(self._buffer[:16]) != header_crc:
                self.header_crc_errors += 1
                del self._buffer[0]
                continue

            if not self._is_valid_header(
                version, packet_type, payload_length, width, height, pixel_format
            ):
                self.invalid_packets += 1
                del self._buffer[0]
                continue

            total_length = HEADER_SIZE + payload_length
            if len(self._buffer) < total_length:
                return packets

            payload = bytes(self._buffer[HEADER_SIZE:total_length])
            del self._buffer[:total_length]
            if self.require_payload_crc and not flags & PACKET_FLAG_PAYLOAD_CRC_PRESENT:
                self.missing_payload_crc_errors += 1
                continue
            if flags & PACKET_FLAG_PAYLOAD_CRC_PRESENT and (
                zlib.crc32(payload) & 0xFFFFFFFF
            ) != payload_crc:
                self.payload_crc_errors += 1
                continue

            packets.append(
                Packet(packet_type, payload, sequence, width, height, pixel_format, flags)
            )

    @staticmethod
    def _is_valid_header(
        version: int,
        packet_type: int,
        payload_length: int,
        width: int,
        height: int,
        pixel_format: int,
    ) -> bool:
        if version != VERSION or payload_length > MAX_PAYLOAD_SIZE:
            return False
        if packet_type == PACKET_IMAGE_RGB565:
            return (
                pixel_format == RGB565_FORMAT
                and width > 0
                and height > 0
                and payload_length == width * height * 2
            )
        if packet_type == PACKET_TEXT:
            return (
                width == 0
                and height == 0
                and pixel_format == 0
                and payload_length <= MAX_TEXT_PAYLOAD_SIZE
            )
        return False


class RawPrintfParser:
    """Line parser for a normal UART printf stream (text-only compatibility)."""

    def __init__(self) -> None:
        self._buffer = bytearray()

    def reset(self) -> None:
        self._buffer.clear()

    def feed(self, incoming: bytes) -> list[DebugMessage]:
        self._buffer.extend(incoming)
        messages: list[DebugMessage] = []
        while True:
            line_end = self._buffer.find(b"\n")
            if line_end < 0:
                # Never retain a damaged endless line indefinitely.
                if len(self._buffer) > MAX_TEXT_PAYLOAD_SIZE:
                    del self._buffer[:-MAX_TEXT_PAYLOAD_SIZE]
                return messages
            line = bytes(self._buffer[: line_end + 1])
            del self._buffer[: line_end + 1]
            message = parse_printf_line(line.decode("utf-8", errors="replace"))
            if message is not None:
                messages.append(message)


def parse_text_payload(payload: bytes) -> list[DebugMessage]:
    """Decode one framed text payload, allowing direct multi-line sends too."""

    text = payload.decode("utf-8", errors="replace")
    messages: list[DebugMessage] = []
    for line in text.splitlines():
        message = parse_printf_line(line)
        if message is not None:
            messages.append(message)
    # A sender may omit the final LF in a direct SubmitText call.
    if text and not text.endswith(("\n", "\r")):
        last_line = text.splitlines()[-1] if text.splitlines() else text
        if not messages or messages[-1].raw_text != last_line.strip():
            message = parse_printf_line(last_line)
            if message is not None:
                messages.append(message)
    return messages


def parse_printf_line(line: str, received_at: datetime | None = None) -> DebugMessage | None:
    """Parse ``[class]describe: value1, value2\n`` safely.

    Non-numeric text (for example ``[info] Base_StartWaitingIMU``) is treated
    as a description-only debug message instead of producing a conversion
    error.  Missing ``[class]`` means the required default level ``info``.
    """

    raw = line.strip("\r\n")
    if not raw.strip():
        return None
    match = _CLASS.match(raw)
    level = match.group(1).lower() if match else "info"
    body = match.group(2).strip() if match else raw.strip()

    if ":" in body:
        description, data_text = body.split(":", 1)
        description = description.strip()
        data_text = data_text.strip()
        values = _parse_values(data_text)
        # A colon identifies a description even if this is a string-only debug
        # line or a currently incomplete/non-numeric data list.
        return DebugMessage(
            level,
            description or "无描述",
            values,
            data_text,
            received_at or datetime.now(),
            raw.strip(),
        )

    values = _parse_values(body)
    if values or _looks_like_numeric_list(body):
        return DebugMessage(
            level,
            "无描述",
            values,
            body,
            received_at or datetime.now(),
            raw.strip(),
        )
    return DebugMessage(
        level,
        body or "无描述",
        (),
        "",
        received_at or datetime.now(),
        raw.strip(),
    )


def _parse_values(data_text: str) -> tuple[float, ...]:
    if not data_text:
        return ()
    items = [item.strip() for item in data_text.split(",")]
    if any(not item for item in items):
        return ()
    try:
        values = tuple(float(item) for item in items)
    except ValueError:
        return ()
    return values if all(math.isfinite(value) for value in values) else ()


def _looks_like_numeric_list(data_text: str) -> bool:
    """Distinguish a legitimate numeric value of 0 from a debug string."""

    if not data_text:
        return False
    return bool(_parse_values(data_text))


def resolve_pixel_layout(layout: str, packet_flags: int = 0) -> str:
    """Resolve automatic layout from the packet's RGB565 byte-order flag."""

    if layout == PIXEL_LAYOUT_AUTO:
        return (
            PIXEL_LAYOUT_RGB565_MSB
            if packet_flags & PIXEL_FLAG_RGB565_MSB_FIRST
            else PIXEL_LAYOUT_RGB565_LSB
        )
    if layout in {
        PIXEL_LAYOUT_RGB565_MSB,
        PIXEL_LAYOUT_RGB565_LSB,
        PIXEL_LAYOUT_BGR565_MSB,
        PIXEL_LAYOUT_BGR565_LSB,
    }:
        return layout
    raise ValueError(f"unknown RGB565 layout: {layout}")


def rgb565_to_rgb888(
    payload: bytes,
    width: int,
    height: int,
    *,
    layout: str = PIXEL_LAYOUT_RGB565_LSB,
    packet_flags: int = 0,
) -> bytes:
    """Convert byte-stream RGB565/BGR565 pixels to RGB888."""

    expected = width * height * 2
    if width <= 0 or height <= 0 or len(payload) != expected:
        raise ValueError("RGB565 payload length does not match image dimensions")

    layout = resolve_pixel_layout(layout, packet_flags)
    msb_first = layout in (PIXEL_LAYOUT_RGB565_MSB, PIXEL_LAYOUT_BGR565_MSB)
    bgr = layout in (PIXEL_LAYOUT_BGR565_MSB, PIXEL_LAYOUT_BGR565_LSB)
    rgb = bytearray(width * height * 3)
    source = memoryview(payload)
    destination = 0
    for offset in range(0, len(payload), 2):
        value = (
            (source[offset] << 8) | source[offset + 1]
            if msb_first
            else source[offset] | (source[offset + 1] << 8)
        )
        red = (value >> 11) & 0x1F
        green = (value >> 5) & 0x3F
        blue = value & 0x1F
        if bgr:
            red, blue = blue, red
        rgb[destination] = (red << 3) | (red >> 2)
        rgb[destination + 1] = (green << 2) | (green >> 4)
        rgb[destination + 2] = (blue << 3) | (blue >> 2)
        destination += 3
    return bytes(rgb)


def rgb565_to_ppm(
    payload: bytes,
    width: int,
    height: int,
    *,
    layout: str = PIXEL_LAYOUT_RGB565_LSB,
    packet_flags: int = 0,
) -> bytes:
    """Convert RGB565/BGR565 data to PPM P6, which Tk PhotoImage reads."""

    rgb = rgb565_to_rgb888(
        payload, width, height, layout=layout, packet_flags=packet_flags
    )
    return rgb888_to_ppm(rgb, width, height)


def rgb888_to_ppm(rgb: bytes, width: int, height: int) -> bytes:
    """Wrap validated RGB888 bytes in a PPM P6 header for Tk PhotoImage."""

    if width <= 0 or height <= 0 or len(rgb) != width * height * 3:
        raise ValueError("RGB888 payload length does not match image dimensions")
    return f"P6\n{width} {height}\n255\n".encode("ascii") + rgb


def fit_size(source_width: int, source_height: int, box_width: int, box_height: int) -> tuple[int, int]:
    """Return the largest whole size that fits a box without changing aspect."""

    if min(source_width, source_height, box_width, box_height) <= 0:
        raise ValueError("image and target dimensions must be positive")
    scale = min(box_width / source_width, box_height / source_height)
    return max(1, round(source_width * scale)), max(1, round(source_height * scale))


def resize_rgb888_nearest(
    rgb: bytes, source_width: int, source_height: int, target_width: int, target_height: int
) -> bytes:
    """Fast dependency-free nearest-neighbor RGB scaling for live preview."""

    if len(rgb) != source_width * source_height * 3:
        raise ValueError("RGB888 payload length does not match source dimensions")
    if min(source_width, source_height, target_width, target_height) <= 0:
        raise ValueError("image dimensions must be positive")
    if (source_width, source_height) == (target_width, target_height):
        return rgb

    source = memoryview(rgb)
    row_width = target_width * 3
    output = bytearray(row_width * target_height)
    x_offsets = [(x * source_width // target_width) * 3 for x in range(target_width)]
    destination_offset = 0
    cached_source_row = -1
    cached_scaled_row = b""
    for y in range(target_height):
        source_row = y * source_height // target_height
        if source_row != cached_source_row:
            base = source_row * source_width * 3
            row = bytearray(row_width)
            for x, source_offset in enumerate(x_offsets):
                target_offset = x * 3
                pixel_offset = base + source_offset
                row[target_offset : target_offset + 3] = source[pixel_offset : pixel_offset + 3]
            cached_scaled_row = bytes(row)
            cached_source_row = source_row
        output[destination_offset : destination_offset + row_width] = cached_scaled_row
        destination_offset += row_width
    return bytes(output)


def rgb888_to_png(rgb: bytes, width: int, height: int) -> bytes:
    """Encode RGB888 as a standards-compliant PNG without third-party modules."""

    expected = width * height * 3
    if width <= 0 or height <= 0 or len(rgb) != expected:
        raise ValueError("RGB888 payload length does not match image dimensions")

    def chunk(kind: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + kind
            + data
            + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
        )

    row_size = width * 3
    scanlines = b"".join(
        b"\x00" + rgb[offset : offset + row_size]
        for offset in range(0, len(rgb), row_size)
    )
    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(
        b"IDAT", zlib.compress(scanlines, level=6)
    ) + chunk(b"IEND", b"")
