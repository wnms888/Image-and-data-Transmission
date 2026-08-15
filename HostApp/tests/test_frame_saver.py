import pathlib
import sys
import tempfile
import time
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from frame_saver import FrameSaveWorker
from protocol import PIXEL_LAYOUT_RGB565_MSB, Packet


class FrameSaverTests(unittest.TestCase):
    def test_current_and_recorded_frames_are_written_as_png(self):
        with tempfile.TemporaryDirectory() as temporary:
            saver = FrameSaveWorker(pathlib.Path(temporary), queue_capacity=4)
            packet = Packet(1, bytes((0xF8, 0x00)), 12, 1, 1, 2, 1)
            current = saver.enqueue_current(packet, PIXEL_LAYOUT_RGB565_MSB)
            recording_directory = saver.start_recording()
            self.assertTrue(saver.enqueue_recorded(packet, PIXEL_LAYOUT_RGB565_MSB))
            saver.stop_recording()

            deadline = time.monotonic() + 2.0
            while saver.stats()[0] < 2 and time.monotonic() < deadline:
                time.sleep(0.01)
            saver.close()

            self.assertIsNotNone(current)
            self.assertTrue(current.exists())
            self.assertTrue((recording_directory / "frame_000001_seq_00012.png").exists())
            self.assertEqual(current.read_bytes()[:8], b"\x89PNG\r\n\x1a\n")


if __name__ == "__main__":
    unittest.main()
