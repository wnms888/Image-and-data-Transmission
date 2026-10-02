import pathlib
import re
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from image_processing import (
    ColourDetectionConfig,
    DetectionColour,
    HslThreshold,
    IpmConfig,
    IpmProcessor,
    detect_colours,
    load_processing_config,
    render_embedded_processing_config,
    rgb_to_hsl240,
    save_embedded_processing_config,
    save_processing_config,
)


class ImageProcessingTests(unittest.TestCase):
    def test_hsl240_and_connected_colour_components_match_defaults(self):
        self.assertEqual(rgb_to_hsl240(255, 0, 0), (0, 240, 120))
        self.assertEqual(rgb_to_hsl240(255, 255, 0), (40, 240, 120))

        width, height = 8, 6
        image = bytearray(width * height * 3)
        for y in range(0, 3):
            for x in range(0, 4):
                image[(y * width + x) * 3 : (y * width + x) * 3 + 3] = bytes((255, 0, 0))
        for y in range(3, 6):
            for x in range(4, 8):
                image[(y * width + x) * 3 : (y * width + x) * 3 + 3] = bytes((255, 255, 0))

        output = detect_colours(
            bytes(image),
            width,
            height,
            ColourDetectionConfig(roi_top_percent=0),
        )
        self.assertEqual((output.red_components, output.yellow_components), (1, 1))
        self.assertEqual(len(output.image), len(image))

    def test_ipm_returns_bird_eye_rgb_image_and_rejects_invalid_range(self):
        source = bytes((12, 34, 56)) * (160 * 128)
        processor = IpmProcessor()
        config = IpmConfig(output_width=48, output_height=40)
        output = processor.process(source, 160, 128, config)
        self.assertEqual(len(output), 48 * 40 * 3)
        self.assertIn(bytes((12, 34, 56)), output)
        with self.assertRaises(ValueError):
            processor.process(source, 160, 128, IpmConfig(near_x=2.0, far_x=0.2))

    def test_processing_configuration_round_trip(self):
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "processing.json"
            ipm = IpmConfig(near_x=0.3, output_width=200)
            colour = ColourDetectionConfig(
                colours=(DetectionColour("红色", HslThreshold(228, 12, 25, 238, 64, 239, area_min=12), (255, 63, 63)),),
                roi_top_percent=30,
            )
            save_processing_config(path, ipm, colour)
            loaded_ipm, loaded_colour = load_processing_config(path)
        self.assertEqual(loaded_ipm, ipm)
        self.assertEqual(loaded_colour, colour)

    def test_embedded_c_export_contains_the_host_tuning(self):
        ipm = IpmConfig(near_x=0.35, far_x=2.75, half_width_y=0.9, output_width=200)
        colour = ColourDetectionConfig(
            colours=(DetectionColour("红色", HslThreshold(228, 12, 25, 238, 64, 239, area_min=12), (255, 63, 63)),),
            roi_top_percent=30,
            roi_left_percent=5,
            roi_right_percent=95,
            maximum_components=9,
        )
        generated = render_embedded_processing_config(ipm, colour)
        self.assertIn('#define TC4_IPM_VIEW_NEAR_X                       (0.35f)', generated)
        self.assertIn('#define TC4_IPM_VIEW_OUTPUT_WIDTH                 (200u)', generated)
        self.assertIn('#define TC4_CFG_COLOR_ROI_LEFT_PERCENT            (5u)', generated)
        self.assertIn('#define TC4_CFG_COLOR_MAX_CONES                   (9u)', generated)
        self.assertIn('#define TC4_CFG_COLOR_COUNT                       (1u)', generated)
        self.assertIn('{(228u), (12u), (25u), (238u), (64u), (239u), (12u),', generated)
        self.assertIn('TC4_IMAGE_PROCESSING_CONFIG_H_', generated)
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / 'TC4ImageProcessingConfig.h'
            save_embedded_processing_config(path, ipm, colour)
            self.assertEqual(path.read_text(encoding='utf-8'), generated)

    def test_repository_embedded_default_header_matches_host_defaults(self):
        pattern = re.compile(r'^#define\s+(TC4_(?:IPM|CFG)_[A-Z0-9_]+)\s+(.+)$', re.MULTILINE)
        generated = dict(pattern.findall(
            render_embedded_processing_config(IpmConfig(), ColourDetectionConfig())
        ))
        embedded_header = pathlib.Path(__file__).resolve().parents[2] / 'EmbedCode' / 'TC4ImageProcessingConfig.h'
        checked_in = dict(pattern.findall(embedded_header.read_text(encoding='utf-8')))
        self.assertEqual(checked_in, generated)

    def test_arbitrary_colours_and_independent_minimum_areas(self):
        width, height = 8, 4
        image = bytearray(width * height * 3)
        for y in range(height):
            for x in range(width):
                image[(y * width + x) * 3:(y * width + x) * 3 + 3] = (
                    bytes((0, 0, 255)) if x < 4 else bytes((0, 255, 0))
                )
        blue = DetectionColour("蓝色", HslThreshold(150, 170, 100, 240, 60, 240, area_min=17,
                                                   width_min=1, height_min=1, aspect_min_permille=0), (20, 60, 255))
        green = DetectionColour("绿色", HslThreshold(70, 90, 100, 240, 60, 240, area_min=8,
                                                     width_min=1, height_min=1, aspect_min_permille=0), (20, 255, 60))
        config = ColourDetectionConfig(colours=(blue, green), roi_top_percent=0)
        result = detect_colours(bytes(image), width, height, config)
        self.assertEqual(result.component_counts, (0, 1))
        self.assertEqual(result.image[:3], bytes((0, 0, 0)))
        self.assertIn(bytes((20, 255, 60)), result.image)

    def test_three_colour_export_preserves_order_and_area(self):
        colours = (
            DetectionColour("蓝", HslThreshold(150, 170, 50, 240, 50, 240, area_min=7), (0, 0, 255)),
            DetectionColour("绿", HslThreshold(70, 90, 50, 240, 50, 240, area_min=13), (0, 255, 0)),
            DetectionColour("紫", HslThreshold(185, 205, 50, 240, 50, 240, area_min=21), (180, 0, 255)),
        )
        config = ColourDetectionConfig(colours=colours)
        header = render_embedded_processing_config(IpmConfig(), config)
        self.assertIn('#define TC4_CFG_COLOR_COUNT                       (3u)', header)
        self.assertLess(header.index('(7u), (12000u)'), header.index('(13u), (12000u)'))
        self.assertLess(header.index('(13u), (12000u)'), header.index('(21u), (12000u)'))
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "three.json"
            save_processing_config(path, IpmConfig(), config)
            _, loaded = load_processing_config(path)
        self.assertEqual(loaded.colours, colours)

    def test_legacy_v1_colour_config_is_imported(self):
        import json
        from dataclasses import asdict
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "old.json"
            old = ColourDetectionConfig()
            path.write_text(json.dumps({
                "format": "IMGT-processing-config", "version": 1,
                "ipm": asdict(IpmConfig()),
                "colour_detection": {
                    "red": asdict(old.colours[0].threshold),
                    "yellow": asdict(old.colours[1].threshold),
                    "roi_top_percent": 25, "roi_left_percent": 0,
                    "roi_right_percent": 100, "maximum_components": 12,
                },
            }), encoding="utf-8")
            _, loaded = load_processing_config(path)
        self.assertEqual(loaded.colours, old.colours)


if __name__ == "__main__":
    unittest.main()
