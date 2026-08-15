"""Background byte readers for TCP and serial inputs."""

from __future__ import annotations

import queue
import socket
import threading
from dataclasses import dataclass
from typing import Literal

try:
    import serial
    from serial.tools import list_ports
except ImportError:  # The GUI remains usable for TCP without pyserial installed.
    serial = None
    list_ports = None

Transport = Literal["tcp_listener", "tcp_client", "serial"]
ReceiverEvent = tuple[str, str | bytes]


@dataclass(frozen=True)
class SerialPortOption:
    """A serial port's stable device name and its user-facing detailed label."""

    device: str
    display_name: str


def _friendly_serial_description(device: str, description: str) -> str:
    """Normalize common Windows descriptions into concise selectable labels."""

    cleaned = (description or "未知设备").strip()
    cleaned = cleaned.replace(f"({device})", "").strip()
    if "bluetooth" in cleaned.lower() or "蓝牙" in cleaned:
        return "蓝牙链接上的标准串行"
    return cleaned or "未知设备"


def available_serial_ports() -> list[SerialPortOption]:
    """Return detailed names such as ``COM8 (蓝牙链接上的标准串行)``."""

    if list_ports is None:
        return []
    options = [
        SerialPortOption(
            port.device,
            f"{port.device} ({_friendly_serial_description(port.device, port.description)})",
        )
        for port in list_ports.comports()
    ]
    return sorted(options, key=lambda option: option.device.upper())


class InputWorker(threading.Thread):
    """Owns a blocking input source and forwards immutable byte chunks to Tk."""

    def __init__(
        self,
        transport: Transport,
        endpoint: str,
        port_or_baud: int,
        events: "queue.Queue[ReceiverEvent]",
    ) -> None:
        super().__init__(name="image-monitor-input", daemon=True)
        self.transport = transport
        self.endpoint = endpoint
        self.port_or_baud = port_or_baud
        self.events = events
        self._stop_event = threading.Event()
        self._active_socket: socket.socket | None = None
        self._active_serial = None

    def request_stop(self) -> None:
        self._stop_event.set()
        for resource in (self._active_socket, self._active_serial):
            if resource is not None:
                try:
                    resource.close()
                except OSError:
                    pass

    def run(self) -> None:
        try:
            if self.transport == "tcp_listener":
                self._run_tcp_listener()
            elif self.transport == "tcp_client":
                self._run_tcp_client()
            else:
                self._run_serial()
        except Exception as exc:  # GUI reports recoverable input configuration errors.
            if not self._stop_event.is_set():
                self.events.put(("error", str(exc)))
        finally:
            self.events.put(("stopped", "输入已停止"))

    def _run_tcp_listener(self) -> None:
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._active_socket = server
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server.bind((self.endpoint, self.port_or_baud))
        server.listen(1)
        server.settimeout(0.25)
        self.events.put(("status", f"正在监听 {self.endpoint}:{self.port_or_baud}"))
        while not self._stop_event.is_set():
            try:
                client, address = server.accept()
            except socket.timeout:
                continue
            self._active_socket = client
            client.settimeout(0.25)
            self.events.put(("status", f"设备已连接：{address[0]}:{address[1]}"))
            self._read_socket(client)
            if not self._stop_event.is_set():
                self.events.put(("status", "设备已断开，继续监听"))
            try:
                client.close()
            except OSError:
                pass
            self._active_socket = server

    def _run_tcp_client(self) -> None:
        self.events.put(("status", f"正在连接 {self.endpoint}:{self.port_or_baud}"))
        client = socket.create_connection((self.endpoint, self.port_or_baud), timeout=5)
        self._active_socket = client
        client.settimeout(0.25)
        self.events.put(("status", f"已连接 {self.endpoint}:{self.port_or_baud}"))
        self._read_socket(client)

    def _read_socket(self, client: socket.socket) -> None:
        while not self._stop_event.is_set():
            try:
                block = client.recv(65536)
            except socket.timeout:
                continue
            if not block:
                return
            self.events.put(("data", block))

    def _run_serial(self) -> None:
        if serial is None:
            raise RuntimeError("未安装 pyserial；请先执行 pip install -r requirements.txt")
        self.events.put(("status", f"正在打开 {self.endpoint} @ {self.port_or_baud}"))
        port = serial.Serial(self.endpoint, self.port_or_baud, timeout=0.2)
        self._active_serial = port
        self.events.put(("status", f"串口已打开：{self.endpoint} @ {self.port_or_baud}"))
        while not self._stop_event.is_set():
            block = port.read(max(1, min(port.in_waiting, 65536)))
            if block:
                self.events.put(("data", block))
