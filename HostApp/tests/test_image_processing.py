import pathlib
import re
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from image_processing import (
    ColourDetectionConfig,
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
                red=HslThreshold(228, 12, 25, 238, 64, 239, area_min=12),
                roi_top_percent=30,
            )
            save_processing_config(path, ipm, colour)
            loaded_ipm, loaded_colour = load_processing_config(path)
        self.assertEqual(loaded_ipm, ipm)
        self.assertEqual(loaded_colour, colour)

    def test_embedded_c_export_contains_the_host_tuning(self):
        ipm = IpmConfig(near_x=0.35, far_x=2.75, half_width_y=0.9, output_width=200)
        colour = ColourDetectionConfig(
            red=HslThreshold(228, 12, 25, 238, 64, 239, area_min=12),
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
        self.assertIn('#define TC4_CFG_RED_H_MIN                         (228u)', generated)
        self.assertIn('#define TC4_CFG_RED_AREA_MIN                      (12u)', generated)
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


if __name__ == "__main__":
    unittest.main()
