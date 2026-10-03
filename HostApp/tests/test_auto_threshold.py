"""Automatic HSL sampling and timestamped export, using actual Tk widgets."""
from concurrent.futures import Future
from dataclasses import asdict
from datetime import datetime, timezone, timedelta
import pathlib
import re
import sys
import tempfile
import time
import tkinter as tk
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from image_processing import (
    ColourDetectionPane, ColourDetectionConfig, HslThreshold, IpmConfig,
    estimate_hsl_threshold, load_processing_config, rgb_to_hsl240, save_processing_config,
)
from monitor import ImageTransmissionApp, timestamped_processing_filename


class AutoThresholdModelTests(unittest.TestCase):
    def setUp(self):
        self.base = HslThreshold(60, 120, 40, 220, 20, 200, area_min=39, area_max=512,
                                 width_min=4, height_min=6)

    def test_red_crossing_zero_and_all_samples_with_noise_margins(self):
        colours = ((255, 0, 20), (255, 20, 0), (220, 0, 5))
        threshold = estimate_hsl_threshold(bytes(c for rgb in colours for c in rgb), 3, 1,
                                           (0, 0, 3, 1), self.base)
        self.assertGreater(threshold.h_min, threshold.h_max)
        for rgb in colours:
            self.assertTrue(threshold.matches(*rgb_to_hsl240(*rgb)))
        old, new = asdict(self.base), asdict(threshold)
        for key in old.keys() - {"h_min", "h_max", "s_min", "s_max", "l_min", "l_max"}:
            self.assertEqual(old[key], new[key])

    def test_only_selected_pixels_and_neutral_hue_range(self):
        raw = bytes((255, 0, 0, 0, 0, 255, 0, 255, 0))
        threshold = estimate_hsl_threshold(raw, 3, 1, (1, 0, 2, 1), self.base)
        self.assertTrue(threshold.matches(*rgb_to_hsl240(0, 0, 255)))
        self.assertFalse(threshold.matches(*rgb_to_hsl240(255, 0, 0)))
        gray = estimate_hsl_threshold(bytes((128, 128, 128)), 1, 1, (0, 0, 1, 1), self.base)
        self.assertEqual((gray.h_min, gray.h_max), (0, 240))
        self.assertTrue(gray.matches(*rgb_to_hsl240(128, 128, 128)))

    def test_invalid_regions_are_rejected(self):
        for box in ((0, 0, 0, 1), (-1, 0, 1, 1), (0, 0, 2, 1), (1, 0, 0, 1)):
            with self.subTest(box=box), self.assertRaises(ValueError):
                estimate_hsl_threshold(bytes((255, 0, 0)), 1, 1, box, self.base)

    def test_time_filename_and_same_minute_collision(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = pathlib.Path(directory)
            now = datetime(2026, 10, 3, 14, 54, tzinfo=timezone(timedelta(hours=8)))
            self.assertEqual(timestamped_processing_filename(folder, now), "imgt_processing_config_261003_1454.json")
            save_processing_config(folder / timestamped_processing_filename(folder, now),
                                   IpmConfig(), ColourDetectionConfig())
            self.assertEqual(timestamped_processing_filename(folder, now),
                             "imgt_processing_config_261003_1454_02.json")


class AutoThresholdWidgetTests(unittest.TestCase):
    def setUp(self):
        try:
            self.root = tk.Tk()
        except tk.TclError as error:
            self.skipTest(f"Tk runtime unavailable: {error}")
        self.root.withdraw()
        self.errors = []
        self.root.report_callback_exception = lambda *args: self.errors.append(args)
        self.changed = []
        self.pane = ColourDetectionPane(self.root, lambda: self.changed.append(True))
        self.pane.pack(fill="both", expand=True)
        self.preview = self.pane.original_preview
        self.rgb = bytes((255, 0, 20)) * 32
        self.preview.set_image(self.rgb, 8, 4)
        # Known letterbox geometry, independent of withdrawn-window canvas size.
        self.preview._display_rectangle = (10, 20, 80, 40)

    def tearDown(self):
        self.pane.shutdown_processing()
        self.root.destroy()
        self.assertFalse(self.errors)

    def wait_estimate(self):
        deadline = time.monotonic() + 3
        while self.pane._auto_threshold_future is not None and time.monotonic() < deadline:
            self.root.update()
            time.sleep(.005)
        self.assertIsNone(self.pane._auto_threshold_future)

    def event(self, name, x=0, y=0):
        self.preview.canvas.event_generate(name, x=x, y=y)

    def invoke_binding(self, sequence):
        # Dispatch the installed Tk callback without mapping/focusing a visible
        # test window on the user's desktop. Motion coordinates are tested below.
        canvas = self.preview.canvas
        command = re.search(r'\[([^\s]+)', canvas.bind(sequence)).group(1)
        values = ["0"] * len(canvas._subst_format)
        values[canvas._subst_format.index("%W")] = str(canvas)
        self.root.tk.call(command, *values)

    def test_buttons_order_freeze_cancel_and_scaled_reverse_drag(self):
        row = self.pane._colour_rows[0]
        actions = row["auto_button"].master.winfo_children()
        self.assertEqual([widget.cget("text") for widget in actions],
                         ["高级阈值…", "框选自动阈值", "移除"])
        row["auto_button"].invoke()
        self.preview.set_image(bytes((0, 0, 255)) * 32, 8, 4)
        self.assertIs(self.preview._rgb, self.rgb)
        self.assertEqual(self.preview._selection_source(0, 0), None)  # black border
        self.preview._selection_press(SimpleNamespace(x=79, y=49))
        self.preview._selection_motion(SimpleNamespace(x=10, y=20))
        self.assertEqual(self.preview._selection_start, (6, 2))
        self.assertEqual(self.preview._selection_end, (0, 0))
        self.assertTrue(self.preview.canvas.find_withtag("threshold_selection"))
        self.preview._selection_release(SimpleNamespace(x=10, y=20))
        self.wait_estimate()
        threshold = self.pane._threshold(row["hsl"], row["geometry"])
        self.assertTrue(threshold.matches(*rgb_to_hsl240(255, 0, 20)))
        self.assertFalse(threshold.matches(*rgb_to_hsl240(0, 0, 255)))
        self.assertFalse(self.preview.canvas.find_withtag("threshold_selection"))
        self.assertEqual(row["geometry"]["area_min"].get(), 10)
        self.assertTrue(self.changed)
        row["auto_button"].invoke()
        self.invoke_binding("<Button-3>")
        self.assertIsNone(self.preview._selection_callback)
        self.assertEqual(row["auto_button"].cget("text"), "框选自动阈值")

    def test_drag_outside_image_is_clipped_and_removed_target_is_cancelled(self):
        row = self.pane._colour_rows[0]
        row["auto_button"].invoke()
        self.preview._selection_press(SimpleNamespace(x=15, y=25))
        self.preview._selection_motion(SimpleNamespace(x=900, y=900))
        self.assertEqual(self.preview._selection_end, (7, 3))
        self.pane._remove_colour(row)
        self.assertIsNone(self.preview._selection_callback)
        self.assertEqual(len(self.pane._colour_rows), 1)

    def test_removed_colour_does_not_receive_late_worker_result(self):
        row = self.pane._colour_rows[0]
        future = Future()
        future.set_result(HslThreshold(150, 170, 30, 240, 30, 240))
        self.pane._auto_threshold_future = future
        self.pane._auto_threshold_target = row
        self.pane._remove_colour(row)
        self.pane._poll_auto_threshold()
        self.assertIsNone(self.pane._auto_threshold_future)
        self.assertEqual(self.pane._colour_rows[0]["hsl"][0].get(), 18)

    def test_escape_binding_cancels_without_changing_thresholds(self):
        row = self.pane._colour_rows[0]
        before = tuple(var.get() for var in row["hsl"])
        row["auto_button"].invoke()
        # Tk's installed binding calls the cancel method; no visible focus needed.
        self.invoke_binding("<Escape>")
        self.assertEqual(tuple(var.get() for var in row["hsl"]), before)
        self.assertIsNone(self.pane._selection_target)

    def test_auto_threshold_repairs_empty_hsl_edit_without_touching_geometry(self):
        row = self.pane._colour_rows[0]
        row["hsl"][0].set("")
        row["geometry"]["area_min"].set(73)
        row["auto_button"].invoke()
        self.preview._selection_press(SimpleNamespace(x=10, y=20))
        self.preview._selection_release(SimpleNamespace(x=89, y=59))
        self.wait_estimate()
        self.assertGreaterEqual(row["hsl"][0].get(), 0)
        self.assertEqual(row["geometry"]["area_min"].get(), 73)


class ExportDirectoryTests(unittest.TestCase):
    def setUp(self):
        try:
            self.root = tk.Tk()
        except tk.TclError as error:
            self.skipTest(f"Tk runtime unavailable: {error}")
        self.root.withdraw()
        self.directory = tempfile.TemporaryDirectory()
        self.folder = pathlib.Path(self.directory.name) / "processing_configs"
        self.state = self.folder / "last_processing_config.json"
        self.app = ImageTransmissionApp(self.root, self.state)
        self.app.processing_config_directory = self.folder

    def tearDown(self):
        self.app.ipm_pane.shutdown_processing()
        self.app.colour_pane.shutdown_processing()
        self.app.frame_saver.close()
        for callback in self.root.tk.call("after", "info"):
            self.root.tk.call("after", "cancel", callback)
        self.root.destroy()
        self.directory.cleanup()

    def test_export_dialog_uses_config_folder_timestamp_and_two_c_copies(self):
        path = self.folder / "imgt_processing_config_261003_1454.json"
        with patch("monitor.timestamped_processing_filename", return_value=path.name):
            with patch("monitor.filedialog.asksaveasfilename", return_value=str(path)) as dialog:
                with patch("monitor.save_embedded_processing_config") as save_header:
                    self.app.export_processing_config()
        self.assertEqual(dialog.call_args.kwargs["initialdir"], str(self.folder))
        self.assertEqual(dialog.call_args.kwargs["initialfile"], path.name)
        self.assertTrue(path.is_file())
        self.assertEqual(len(save_header.call_args_list), 2)
        self.assertEqual(save_header.call_args_list[0].args[0],
                         path.with_name(path.stem + "_embedded_config.h"))
        self.assertEqual(load_processing_config(path)[1], self.app._colour_config_for_export())

    def test_import_defaults_to_dedicated_folder(self):
        with patch("monitor.filedialog.askopenfilename", return_value="") as dialog:
            self.app.import_processing_config()
        self.assertEqual(dialog.call_args.kwargs["initialdir"], str(self.folder))

    def test_old_state_remains_readable_without_moving_user_file(self):
        old = self.folder.parent / "last_processing_config.json"
        save_processing_config(old, IpmConfig(near_x=.37), ColourDetectionConfig())
        with patch("monitor.__file__", str(self.folder.parent / "monitor.py")):
            self.app._restore_processing_state()
        self.assertEqual(self.app.ipm_pane.near_x_var.get(), .37)
        self.assertTrue(old.exists())


if __name__ == "__main__":
    unittest.main()
