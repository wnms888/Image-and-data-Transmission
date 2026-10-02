"""Session persistence and responsive exit, exercised against real Tk widgets."""

import pathlib
import sys
import tempfile
import time
import tkinter as tk
import unittest
from dataclasses import replace
from unittest.mock import patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from image_processing import ColourDetectionConfig, IpmConfig, load_processing_config, save_processing_config
from monitor import ImageTransmissionApp
from protocol import Packet, PIXEL_LAYOUT_RGB565_MSB
from frame_saver import rgb888_to_png


class SessionTests(unittest.TestCase):
    def setUp(self):
        try:
            self.root = tk.Tk()
        except tk.TclError as error:
            self.skipTest(f'Tk runtime unavailable: {error}')
        self.root.withdraw()
        self.directory = tempfile.TemporaryDirectory()
        self.path = pathlib.Path(self.directory.name) / 'last_processing_config.json'
        self.initial_ipm = IpmConfig(near_x=.31, far_x=2.6, output_width=160)
        self.initial_colour = replace(ColourDetectionConfig(), roi_top_percent=37)
        save_processing_config(self.path, self.initial_ipm, self.initial_colour)
        self.callback_errors = []
        self.root.report_callback_exception = lambda *args: self.callback_errors.append(args)
        self.app = ImageTransmissionApp(self.root, self.path)
        self.app.frame_saver.base_directory = self.path.parent / 'frames'

    def tearDown(self):
        if self.app._closing:
            self.app._shutdown_complete.wait(5)
        else:
            self.app.frame_saver.close()
        try:
            for callback in self.root.tk.call('after', 'info'):
                self.root.after_cancel(callback)
            self.root.destroy()
        except tk.TclError:
            pass
        self.directory.cleanup()
        self.assertFalse(self.callback_errors)

    def wait_closed(self):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            try:
                if not self.root.winfo_exists():
                    return
                self.root.update()
            except tk.TclError:
                return
            time.sleep(.005)
        self.fail('exit did not finish')

    def test_exit_button_saves_uncommitted_edits_and_next_launch_restores(self):
        self.assertEqual(self.app.ipm_pane.get_config(), self.initial_ipm)
        self.assertEqual(self.app.colour_pane.get_config(), self.initial_colour)
        self.app.ipm_pane.near_x_var.set(.42)
        self.app.colour_pane._add_colour()
        row = self.app.colour_pane._colour_rows[-1]
        row['hsl'][0].set(151)
        row['geometry']['area_min'].set(27)
        expected = (self.app.ipm_pane.get_config(), self.app.colour_pane.get_config())
        self.app.exit_button.invoke()
        self.assertEqual(load_processing_config(self.path), expected)
        self.wait_closed()
        self.root = tk.Tk()
        self.root.withdraw()
        self.root.report_callback_exception = lambda *args: self.callback_errors.append(args)
        self.app = ImageTransmissionApp(self.root, self.path)
        self.assertEqual((self.app.ipm_pane.get_config(), self.app.colour_pane.get_config()), expected)
        # The window-close button invokes the same save-before-exit path.
        self.root.tk.call(self.root.protocol('WM_DELETE_WINDOW'))
        self.wait_closed()

    def test_invalid_edit_and_write_error_keep_window_and_previous_save(self):
        previous = self.path.read_bytes()
        variable = self.app.colour_pane._colour_rows[0]['geometry']['area_min']
        variable.set('')
        with patch('monitor.messagebox.showerror') as show_error:
            self.app.close()
            show_error.assert_called_once()
        self.assertFalse(self.app._closing)
        self.assertEqual(self.path.read_bytes(), previous)
        variable.set(10)
        with patch('monitor.save_processing_config', side_effect=PermissionError('read-only disk')):
            with patch('monitor.messagebox.showerror') as show_error:
                self.app.close()
                show_error.assert_called_once()
        self.assertFalse(self.app._closing)
        self.assertEqual(self.path.read_bytes(), previous)

    def test_exit_is_responsive_and_finishes_queued_pngs(self):
        recording = self.app.frame_saver.start_recording()
        def slow_png(*args):
            time.sleep(.08)
            return rgb888_to_png(*args)
        ticks = []
        def heartbeat():
            ticks.append(time.monotonic())
            self.root.after(10, heartbeat)
        with patch('frame_saver.rgb888_to_png', side_effect=slow_png):
            for sequence in range(6):
                self.app.frame_saver.enqueue_recorded(
                    Packet(1, bytes((0xF8, 0)), sequence, 1, 1, 2), PIXEL_LAYOUT_RGB565_MSB)
            self.root.after(10, heartbeat)
            self.app.close()
            self.assertTrue(self.path.exists())
            self.wait_closed()
        self.assertGreater(len(ticks), 10)
        self.assertEqual(len(list(recording.glob('*.png'))), 6)
        self.assertEqual(self.app.frame_saver.stats()[0], 6)


if __name__ == '__main__':
    unittest.main()
