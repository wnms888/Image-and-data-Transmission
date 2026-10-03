"""Compile the REAL embedded modules; compare C images and packets with Python.

Only TC4ImageProcessingConfig.h and camera-driver dimensions vary across builds.
MSVC desktop tests validate algorithms, not AURIX timing/DMA/electrical hardware.
"""
import ctypes
from contextlib import contextmanager
from dataclasses import replace
import os
from pathlib import Path
import random
import shutil
import struct
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from image_processing import (
    CameraCalibration, ColourDetectionConfig, DetectionColour, HslThreshold,
    IpmConfig, IpmProcessor, detect_colours, load_processing_config,
    rgb_to_hsl240, save_embedded_processing_config,
)
from protocol import (
    PIXEL_LAYOUT_AUTO, StreamParser, resolve_pixel_layout, rgb565_to_rgb888,
)

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = Path(__file__).resolve().parent / "embedded_fixture"


def compiler_command(directory, width, height):
    sources = ["CameraIPM.c", "ColorDetection.c", "WifiImageTransfer.c",
               str(FIXTURES / "processing_bridge.c")]
    if os.name == "nt":
        vswhere = Path(os.environ.get("ProgramFiles(x86)", "C:/Program Files (x86)")) / "Microsoft Visual Studio/Installer/vswhere.exe"
        if not vswhere.exists():
            raise unittest.SkipTest("Visual Studio C compiler is unavailable")
        paths = subprocess.check_output(
            [str(vswhere), "-latest", "-products", "*", "-requires",
             "Microsoft.VisualStudio.Component.VC.Tools.x86.x64",
             "-find", "VC/Auxiliary/Build/vcvars64.bat"], text=True).strip().splitlines()
        if not paths:
            raise unittest.SkipTest("Visual Studio C build tools are unavailable")
        args = ["cl", "/nologo", "/std:c11", "/O2", "/fp:strict", "/W4", "/WX", "/LD",
                f"/DSCC8660_W={width}", f"/DSCC8660_H={height}",
                f"/I{directory}", f"/I{FIXTURES}", *sources, "/link", "/OUT:parity.dll"]
        # cmd parses quotes differently from an argv-style Windows executable.
        # Pass its command string directly; list2cmdline on the whole /c
        # argument would turn the batch path quotes into literal backslashes.
        command = f'call "{paths[0]}" >nul && ' + subprocess.list2cmdline(args)
        return f'cmd.exe /d /s /c "{command}"', "parity.dll"
    cc = shutil.which("cc")
    if not cc:
        raise unittest.SkipTest("A desktop C compiler is unavailable")
    return [cc, "-std=c11", "-shared", "-fPIC", "-O2", "-ffp-contract=off",
            "-Wall", "-Wextra", "-Werror",
            f"-DSCC8660_W={width}", f"-DSCC8660_H={height}",
            f"-I{directory}", f"-I{FIXTURES}", *sources, "-lm", "-o", "parity.so"], "parity.so"


@contextmanager
def compiled_target(ipm, colour, width=160, height=128):
    with tempfile.TemporaryDirectory(prefix="imgt-parity-") as temporary:
        directory = Path(temporary)
        for name in ("CameraIPM", "ColorDetection", "WifiImageTransfer"):
            for extension in (".c", ".h"):
                shutil.copy2(ROOT / "EmbedCode" / (name + extension), directory)
        save_embedded_processing_config(directory / "TC4ImageProcessingConfig.h", ipm, colour)
        command, library = compiler_command(directory, width, height)
        build = subprocess.run(command, cwd=directory, capture_output=True, text=True,
                               encoding="utf-8", errors="replace", timeout=60)
        if build.returncode:
            raise AssertionError("Embedded C compilation failed:\n" + build.stdout + build.stderr)
        dll = ctypes.CDLL(str(directory / library))
        signatures = {
            "Test_Color": ([ctypes.c_void_p, ctypes.c_void_p], ctypes.c_uint32),
            "Test_Count": ([ctypes.c_uint32], ctypes.c_uint32),
            "Test_WhiteArea": ([], ctypes.c_uint32),
            "Test_WhiteComponents": ([], ctypes.c_uint32),
            "Test_RoiPixels": ([], ctypes.c_uint32),
            "Test_Ipm": ([ctypes.c_void_p, ctypes.c_void_p], ctypes.c_uint32),
            "Test_AllHsl": ([ctypes.c_void_p], None),
            "Test_ImagePacket": ([ctypes.c_void_p, ctypes.c_void_p], ctypes.c_uint32),
            "Test_TextPacket": ([ctypes.c_void_p, ctypes.c_uint32, ctypes.c_void_p], ctypes.c_uint32),
        }
        for name, (arguments, result) in signatures.items():
            function = getattr(dll, name)
            function.argtypes, function.restype = arguments, result
        try:
            yield dll
        finally:
            if os.name == "nt":
                import _ctypes
                _ctypes.FreeLibrary(dll._handle)


def pack_words(words, layout):
    return struct.pack((">" if layout.endswith("_msb") else "<") + "H" * len(words), *words)


def permissive_rule(h_min=0, h_max=240, preview=(40, 190, 255)):
    return DetectionColour("test", HslThreshold(
        h_min, h_max, 0, 240, 0, 240, area_min=1, area_max=65535,
        width_min=1, height_min=1, fill_min_permille=0,
        aspect_min_permille=0, aspect_max_permille=65535), preview)


class EmbeddedParityTests(unittest.TestCase):
    def assert_colour_matches(self, dll, raw, width, height, config):
        rgb = rgb565_to_rgb888(raw, width, height, layout=config.pixel_layout)
        expected = detect_colours(rgb, width, height, config)
        output = ctypes.create_string_buffer(width * height * 3)
        dll.Test_Color(raw, output)
        self.assertEqual(output.raw, expected.image)
        self.assertEqual(tuple(dll.Test_Count(i) for i in range(len(config.colours))),
                         expected.component_counts)
        return expected

    def test_every_rgb565_word_hsl_and_wire_flags_in_four_layouts(self):
        for layout in ("rgb565_msb", "rgb565_lsb", "bgr565_msb", "bgr565_lsb"):
            config = replace(ColourDetectionConfig(), pixel_layout=layout)
            with self.subTest(layout=layout), compiled_target(IpmConfig(), config) as dll:
                # C receives the numeric WORD as stored in little-endian CPU memory.
                numeric_words = struct.pack("<65536H", *range(65536))
                rgb = rgb565_to_rgb888(numeric_words, 256, 256, layout=layout)
                expected = bytes(channel for offset in range(0, len(rgb), 3)
                                 for channel in rgb_to_hsl240(*rgb[offset:offset + 3]))
                output = ctypes.create_string_buffer(65536 * 3)
                dll.Test_AllHsl(output)
                self.assertEqual(output.raw, expected)
                raw = pack_words([0xF800, 0x07E0, 0x001F, 0xFFFF] * (160 * 128 // 4), layout)
                wire = ctypes.create_string_buffer(len(raw) + 65536)
                count = dll.Test_ImagePacket(raw, wire)
                packets = StreamParser().feed(wire.raw[:count])
                self.assertEqual(len(packets), 1)
                self.assertEqual(resolve_pixel_layout(PIXEL_LAYOUT_AUTO, packets[0].flags), layout)
                self.assertEqual(packets[0].payload, raw)
                self.assertEqual(rgb565_to_rgb888(packets[0].payload, 160, 128, layout=PIXEL_LAYOUT_AUTO,
                                                 packet_flags=packets[0].flags),
                                 rgb565_to_rgb888(raw, 160, 128, layout=layout))
                text = b"[warning]speed:12,13\n"
                count = dll.Test_TextPacket(text, len(text), wire)
                self.assertEqual(StreamParser().feed(wire.raw[:count])[0].payload, text)
                broken = bytearray(wire.raw[:count])
                broken[-1] ^= 1
                parser = StreamParser()
                self.assertEqual(parser.feed(broken), [])
                self.assertEqual(parser.payload_crc_errors, 1)

    def test_saved_four_colour_configuration_and_default_ipm_match_pixel_for_pixel(self):
        ipm, config = load_processing_config(ROOT / "HostApp/imgt_processing_config.json")
        rng = random.Random(41)
        words = [rng.randrange(65536) for _ in range(160 * 128)]
        # Large solid regions exercise accepted components, not just random noise.
        for y in range(45, 85):
            for x in range(10, 150):
                words[y * 160 + x] = (0xFD20, 0x001F, 0x801F, 0x0000)[(x - 10) // 35]
        raw = pack_words(words, config.pixel_layout)
        with compiled_target(ipm, config) as dll:
            result = self.assert_colour_matches(dll, raw, 160, 128, config)
            self.assertTrue(any(result.component_counts))
            output = ctypes.create_string_buffer(ipm.output_width * ipm.output_height * 2)
            self.assertEqual(dll.Test_Ipm(raw, output), 1)
            expected = IpmProcessor().process(
                rgb565_to_rgb888(raw, 160, 128, layout=config.pixel_layout), 160, 128, ipm)
            self.assertEqual(rgb565_to_rgb888(output.raw, ipm.output_width, ipm.output_height,
                                             layout=config.pixel_layout), expected)

    def test_roi_half_up_rounding_colour_priority_and_component_cap(self):
        width, height = 129, 126
        config = ColourDetectionConfig(
            colours=(permissive_rule(230, 15, (255, 11, 30)),
                     permissive_rule(0, 20, (11, 255, 30)),
                     permissive_rule(150, 175, (11, 30, 255))),
            roi_top_percent=25, roi_left_percent=50, roi_right_percent=99,
            maximum_components=2, pixel_layout="bgr565_lsb")
        words = [0x07E0] * (width * height)
        for y in (32, 42, 52, 62):
            for yy in range(y, y + 5):
                for xx in range(65, 70):
                    words[yy * width + xx] = 0x001F  # BGR red, first rule wins.
                for xx in range(80, 86):
                    words[yy * width + xx] = 0xF800  # BGR blue.
        raw = pack_words(words, config.pixel_layout)
        with compiled_target(IpmConfig(output_width=48, output_height=40), config, width, height) as dll:
            result = self.assert_colour_matches(dll, raw, width, height, config)
            self.assertEqual(result.component_counts, (2, 0, 2))
            self.assertEqual(dll.Test_RoiPixels(), (126 - 32) * (128 - 65))

    def test_empty_rounded_roi_and_independent_white_roi(self):
        config = ColourDetectionConfig(colours=(permissive_rule(),),
                                       roi_top_percent=90, roi_left_percent=1,
                                       roi_right_percent=2)
        with compiled_target(IpmConfig(output_width=8, output_height=8), config, 8, 8) as dll:
            self.assert_colour_matches(dll, pack_words([0xFFFF] * 64, config.pixel_layout), 8, 8, config)
            self.assertEqual(dll.Test_RoiPixels(), 0)
        config = replace(config, roi_top_percent=90, roi_left_percent=0, roi_right_percent=100)
        with compiled_target(IpmConfig(output_width=8, output_height=8), config, 16, 16) as dll:
            self.assert_colour_matches(dll, pack_words([0xFFFF] * 256, config.pixel_layout), 16, 16, config)
            self.assertEqual(dll.Test_WhiteArea(), 16 * 12)
            self.assertEqual(dll.Test_WhiteComponents(), 1)

    def test_large_frame_indexes_and_area_do_not_wrap(self):
        width, height = 400, 256
        config = ColourDetectionConfig(colours=(permissive_rule(),), roi_top_percent=0)
        words = [0xFFFF] * (width * height)
        with compiled_target(IpmConfig(output_width=8, output_height=8), config, width, height) as dll:
            result = self.assert_colour_matches(dll, pack_words(words, config.pixel_layout), width, height, config)
            self.assertEqual(result.component_counts, (0,))  # exceeds area_max, NOT truncated
            self.assertEqual(dll.Test_WhiteArea(), width * (height - 64))
            self.assertEqual(dll.Test_WhiteComponents(), 1)

    def test_ipm_six_radial_coefficients_tangential_and_header_only_resize(self):
        calibration = replace(CameraCalibration(), radial_count=6,
                              k=(.03, -.04, .01, .015, -.012, .007), p1=.004, p2=-.002)
        ipm = IpmConfig(near_x=.12345678901234567, far_x=3.75, half_width_y=1.4,
                        output_width=81, output_height=73, calibration=calibration)
        config = replace(ColourDetectionConfig(), pixel_layout="rgb565_lsb")
        width, height = 188, 120
        raw = pack_words([(y * width + x) % 65536 for y in range(height) for x in range(width)],
                         config.pixel_layout)
        with compiled_target(ipm, config, width, height) as dll:
            output = ctypes.create_string_buffer(ipm.output_width * ipm.output_height * 2)
            self.assertEqual(dll.Test_Ipm(raw, output), 1)
            expected = IpmProcessor().process(
                rgb565_to_rgb888(raw, width, height, layout=config.pixel_layout), width, height, ipm)
            self.assertEqual(rgb565_to_rgb888(output.raw, ipm.output_width, ipm.output_height,
                                             layout=config.pixel_layout), expected)

    def test_ipm_ties_to_even_orientation_and_black_invalid_pixels(self):
        # Identity H plus .5 ground increments hits exact nearest-pixel ties.
        calibration = CameraCalibration(
            reference_width=8, reference_height=8, fx=1.0, fy=1.0,
            cx=0.0, cy=0.0, k=(0.0,) * 6,
            homography=(1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0))
        ipm = IpmConfig(near_x=.5, far_x=4.0, half_width_y=1.75,
                        output_width=8, output_height=8, calibration=calibration)
        config = ColourDetectionConfig()
        raw = pack_words([0xF800 | index for index in range(64)], config.pixel_layout)
        with compiled_target(ipm, config, 8, 8) as dll:
            output = ctypes.create_string_buffer(128)
            self.assertEqual(dll.Test_Ipm(raw, output), 1)
            rgb = rgb565_to_rgb888(raw, 8, 8, layout=config.pixel_layout)
            expected = IpmProcessor().process(rgb, 8, 8, ipm)
            self.assertEqual(rgb565_to_rgb888(output.raw, 8, 8, layout=config.pixel_layout), expected)
            # Bottom row gx=.5 selects even u=0; gx=1.5 selects even u=2.
            self.assertEqual(expected[(7 * 8) * 3:(7 * 8) * 3 + 3], rgb[16 * 3:16 * 3 + 3])
            self.assertIn(bytes((0, 0, 0)), expected)

    def test_more_than_254_colours_only_changes_header_and_expands_label_type(self):
        rules = tuple(replace(permissive_rule(80, 80), name=f"unused {i}") for i in range(259))
        rules += (replace(permissive_rule(150, 170), name="blue last"),)
        config = ColourDetectionConfig(colours=rules, roi_top_percent=0, maximum_components=1)
        with compiled_target(IpmConfig(output_width=8, output_height=8), config, 8, 8) as dll:
            result = self.assert_colour_matches(dll, pack_words([0x001F] * 64, config.pixel_layout), 8, 8, config)
            self.assertEqual(result.component_counts[-1], 1)
            self.assertEqual(sum(result.component_counts), 1)


if __name__ == "__main__":
    unittest.main()
