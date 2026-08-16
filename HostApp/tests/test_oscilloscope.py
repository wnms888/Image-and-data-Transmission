import pathlib
import sys
import unittest
from datetime import datetime, timedelta

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from oscilloscope import ScopeModel
from protocol import DebugMessage


class ScopeModelTests(unittest.TestCase):
    def test_capture_zoom_autoscale_and_clear(self):
        model = ScopeModel(history_seconds=30.0, max_samples=10)
        timestamp = datetime.now()
        message = DebugMessage(
            "info", "imu", (1.0, -2.0), "1,-2", timestamp, "imu:1,-2"
        )
        self.assertFalse(model.add_message(message))

        model.enabled = True
        self.assertTrue(model.add_message(message))
        self.assertTrue(
            model.add_message(
                DebugMessage(
                    "info",
                    "imu",
                    (3.0, 4.0),
                    "3,4",
                    timestamp + timedelta(seconds=1),
                    "imu:3,4",
                )
            )
        )
        _end_time, visible = model.visible_samples()
        self.assertEqual(len(visible), 2)
        self.assertEqual(model.y_limits(visible), (-2.6, 4.6))

        model.zoom_x(1 / 1.6)
        model.zoom_y(1 / 1.6, visible)
        self.assertFalse(model.automatic_y_scale)
        self.assertLess(model.time_window_seconds, 10.0)
        model.auto_scale()
        self.assertTrue(model.automatic_y_scale)
        model.paused = True
        self.assertFalse(model.add_message(message))
        model.paused = False
        model.clear()
        self.assertEqual(model.visible_samples()[1], [])

    def test_pause_and_time_axis_pan_keep_a_historical_view(self):
        model = ScopeModel(history_seconds=40.0, max_samples=20)
        model.enabled = True
        timestamp = datetime.now()
        for seconds in (0, 10, 20, 30):
            self.assertTrue(
                model.add_message(
                    DebugMessage(
                        "info", "data", (float(seconds),), str(seconds),
                        timestamp + timedelta(seconds=seconds), str(seconds),
                    )
                )
            )
        self.assertAlmostEqual(model.view_end_timestamp(), timestamp.timestamp() + 30)
        model.pan(-5)
        self.assertAlmostEqual(model.view_end_timestamp(), timestamp.timestamp() + 25)
        self.assertLess(model.time_position(), 1.0)
        model.set_time_position(0.0)
        self.assertAlmostEqual(model.view_end_timestamp(), timestamp.timestamp() + 10)
        model.follow_latest()
        self.assertAlmostEqual(model.view_end_timestamp(), timestamp.timestamp() + 30)
        model.paused = True
        self.assertFalse(
            model.add_message(
                DebugMessage("info", "data", (99.0,), "99", timestamp, "99")
            )
        )


if __name__ == "__main__":
    unittest.main()
