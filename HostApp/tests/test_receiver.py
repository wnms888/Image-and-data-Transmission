import pathlib
import sys
import types
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import receiver


class SerialPortPresentationTests(unittest.TestCase):
    def test_detailed_windows_labels(self):
        previous = receiver.list_ports
        receiver.list_ports = types.SimpleNamespace(
            comports=lambda: [
                types.SimpleNamespace(device="COM24", description="Infineon DAS JDS COM (COM24)"),
                types.SimpleNamespace(device="COM8", description="Standard Serial over Bluetooth link (COM8)"),
            ]
        )
        try:
            labels = [option.display_name for option in receiver.available_serial_ports()]
        finally:
            receiver.list_ports = previous
        self.assertEqual(
            labels,
            ["COM24 (Infineon DAS JDS COM)", "COM8 (蓝牙链接上的标准串行)"],
        )


if __name__ == "__main__":
    unittest.main()
