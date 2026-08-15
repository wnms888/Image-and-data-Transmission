"""Modern, resizable desktop monitor for IMGT image and Printf data streams."""

from __future__ import annotations

from collections import deque
from datetime import datetime
from pathlib import Path
import queue
import tkinter as tk
from time import perf_counter
from tkinter import messagebox, ttk

from frame_saver import FrameSaveWorker
from protocol import (
    PACKET_IMAGE_RGB565,
    PACKET_TEXT,
    PIXEL_LAYOUT_AUTO,
    PIXEL_LAYOUT_BGR565_LSB,
    PIXEL_LAYOUT_BGR565_MSB,
    PIXEL_LAYOUT_RGB565_LSB,
    PIXEL_LAYOUT_RGB565_MSB,
    Packet,
    RawPrintfParser,
    StreamParser,
    fit_size,
    parse_text_payload,
    resize_rgb888_nearest,
    rgb565_to_rgb888,
    rgb888_to_ppm,
)
from receiver import InputWorker, available_serial_ports


TRANSPORTS = {
    "TCP 监听（开发板连接本机）": "tcp_listener",
    "TCP 客户端（连接远端）": "tcp_client",
    "串口": "serial",
}
FRAMED_PROTOCOL = "IMGT v1（二进制图像 + Printf）"
RAW_PROTOCOL = "原始 Printf 文本（仅日志）"
PIXEL_LAYOUTS = {
    "自动（读取包头字节序）": PIXEL_LAYOUT_AUTO,
    "RGB565 · 高字节优先（SCC8660 推荐）": PIXEL_LAYOUT_RGB565_MSB,
    "RGB565 · 低字节优先": PIXEL_LAYOUT_RGB565_LSB,
    "BGR565 · 高字节优先": PIXEL_LAYOUT_BGR565_MSB,
    "BGR565 · 低字节优先": PIXEL_LAYOUT_BGR565_LSB,
}


class LogPane(ttk.Frame):
    def __init__(self, parent: tk.Misc, title: str, foreground: str) -> None:
        super().__init__(parent, padding=(10, 8))
        self.columnconfigure(0, weight=1)
        self.rowconfigure(1, weight=1)
        ttk.Label(self, text=title, style="Section.TLabel", foreground=foreground).grid(
            row=0, column=0, sticky="w", pady=(0, 6)
        )
        self.text = tk.Text(
            self,
            height=8,
            wrap="word",
            borderwidth=0,
            highlightthickness=0,
            padx=10,
            pady=8,
            background="#111c31",
            foreground="#dbeafe",
            insertbackground="#dbeafe",
            font=("Microsoft YaHei UI", 10),
            state="disabled",
        )
        self.text.grid(row=1, column=0, sticky="nsew")
        scrollbar = ttk.Scrollbar(self, orient="vertical", command=self.text.yview)
        scrollbar.grid(row=1, column=1, sticky="ns")
        self.text.configure(yscrollcommand=scrollbar.set)

    def append(self, text: str) -> None:
        self.text.configure(state="normal")
        self.text.insert("end", text + "\n")
        # Keep the GUI responsive during long-running telemetry sessions.
        if int(self.text.index("end-1c").split(".")[0]) > 1200:
            self.text.delete("1.0", "201.0")
        self.text.see("end")
        self.text.configure(state="disabled")

    def clear(self) -> None:
        self.text.configure(state="normal")
        self.text.delete("1.0", "end")
        self.text.configure(state="disabled")


class ImageTransmissionApp:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title("IMGT 图像与调试监视器")
        self.root.minsize(960, 640)
        self.root.geometry("1280x800")
        self._configure_style()

        self.events: "queue.Queue[tuple[str, str | bytes]]" = queue.Queue()
        self.worker: InputWorker | None = None
        self.stream_parser = StreamParser()
        self.raw_parser = RawPrintfParser()
        self.frame_saver = FrameSaveWorker(Path(__file__).resolve().parent / "captured_frames")
        self._serial_display_to_device: dict[str, str] = {}
        self.photo: tk.PhotoImage | None = None
        self.image_item: int | None = None
        self.last_image_packet: Packet | None = None
        self._source_rgb = b""
        self._source_rgb_key: tuple[int, str, int] | None = None
        self._resize_render_pending = False
        self._preview_dirty = False
        self._frame_times: deque[float] = deque()
        self.frame_rate = 0.0
        self.lost_packet_count = 0
        self._last_sequence: int | None = None
        self.packet_count = 0
        self.image_count = 0
        self.log_count = 0

        self.transport_var = tk.StringVar(value="TCP 监听（开发板连接本机）")
        self.protocol_var = tk.StringVar(value=FRAMED_PROTOCOL)
        self.pixel_layout_var = tk.StringVar(value="RGB565 · 高字节优先（SCC8660 推荐）")
        self.endpoint_var = tk.StringVar(value="0.0.0.0")
        self.port_var = tk.StringVar(value="8086")
        self.endpoint_label_var = tk.StringVar(value="监听地址")
        self.port_label_var = tk.StringVar(value="TCP 端口")
        self.status_var = tk.StringVar(value="未连接")
        self.stats_var = tk.StringVar(value="数据包 0 · 图像 0 · 日志 0 · FPS 0.0")
        self.image_info_var = tk.StringVar(value="等待 RGB565 图像帧")
        self.storage_info_var = tk.StringVar(value="图像默认保存为 PNG 到 captured_frames 文件夹")

        self._build_layout()
        self.pixel_layout_var.trace_add("write", self._on_pixel_layout_change)
        self._on_transport_change()
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        self.root.after(30, self._drain_events)

    def _configure_style(self) -> None:
        style = ttk.Style(self.root)
        style.theme_use("clam")
        self.root.configure(background="#0b1220")
        style.configure("TFrame", background="#0b1220")
        style.configure("Card.TFrame", background="#111c31")
        style.configure("TLabelframe", background="#111c31", foreground="#dbeafe")
        style.configure("TLabelframe.Label", background="#111c31", foreground="#93c5fd", font=("Microsoft YaHei UI", 10, "bold"))
        style.configure("TLabel", background="#0b1220", foreground="#dbeafe", font=("Microsoft YaHei UI", 10))
        style.configure("Section.TLabel", background="#111c31", font=("Microsoft YaHei UI", 10, "bold"))
        style.configure("TButton", padding=(12, 7), font=("Microsoft YaHei UI", 10, "bold"))
        style.configure("Accent.TButton", background="#2563eb", foreground="#ffffff")
        style.map("Accent.TButton", background=[("active", "#1d4ed8")])
        # Explicit dark readonly fields avoid Windows' default white combobox
        # background, which is too close to the light text used by this theme.
        style.configure(
            "Input.TEntry",
            fieldbackground="#172554",
            foreground="#f8fafc",
            insertcolor="#f8fafc",
            padding=6,
        )
        style.configure(
            "Input.TCombobox",
            fieldbackground="#172554",
            background="#172554",
            foreground="#f8fafc",
            arrowcolor="#bfdbfe",
            padding=5,
        )
        style.map(
            "Input.TCombobox",
            fieldbackground=[("readonly", "#172554"), ("disabled", "#1e293b")],
            background=[("readonly", "#172554")],
            foreground=[("readonly", "#f8fafc"), ("disabled", "#94a3b8")],
            selectbackground=[("readonly", "#1d4ed8")],
            selectforeground=[("readonly", "#ffffff")],
        )
        style.configure("Connected.TLabel", foreground="#5eead4", font=("Microsoft YaHei UI", 10, "bold"))
        style.configure("Disconnected.TLabel", foreground="#fbbf24", font=("Microsoft YaHei UI", 10, "bold"))

    def _build_layout(self) -> None:
        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(2, weight=1)

        header = ttk.Frame(self.root, style="Card.TFrame", padding=(18, 12))
        header.grid(row=0, column=0, sticky="ew", padx=14, pady=(14, 8))
        header.columnconfigure(1, weight=1)
        ttk.Label(header, text="IMGT", style="Section.TLabel", font=("Microsoft YaHei UI", 18, "bold"), foreground="#60a5fa").grid(row=0, column=0, sticky="w")
        ttk.Label(header, text="图像传输与 Printf 调试监视器", style="Section.TLabel", foreground="#cbd5e1").grid(row=0, column=1, sticky="w", padx=(12, 0))
        self.status_label = ttk.Label(header, textvariable=self.status_var, style="Disconnected.TLabel")
        self.status_label.grid(row=0, column=2, sticky="e")

        connection = ttk.LabelFrame(self.root, text=" 输入配置 ", padding=(14, 10))
        connection.grid(row=1, column=0, sticky="new", padx=14)
        for column in (1, 3, 5):
            connection.columnconfigure(column, weight=1)
        ttk.Label(connection, text="输入方式").grid(row=0, column=0, sticky="w", padx=(0, 6), pady=4)
        self.transport_box = ttk.Combobox(
            connection,
            textvariable=self.transport_var,
            state="readonly",
            values=list(TRANSPORTS),
            width=24,
            style="Input.TCombobox",
        )
        self.transport_box.grid(row=0, column=1, sticky="ew", padx=(0, 14), pady=4)
        self.transport_box.bind("<<ComboboxSelected>>", lambda _event: self._on_transport_change())
        ttk.Label(connection, textvariable=self.endpoint_label_var).grid(row=0, column=2, sticky="w", padx=(0, 6), pady=4)
        self.endpoint_box = ttk.Combobox(
            connection, textvariable=self.endpoint_var, width=22, style="Input.TCombobox"
        )
        self.endpoint_box.grid(row=0, column=3, sticky="ew", padx=(0, 14), pady=4)
        ttk.Label(connection, textvariable=self.port_label_var).grid(row=0, column=4, sticky="w", padx=(0, 6), pady=4)
        self.port_entry = ttk.Entry(
            connection, textvariable=self.port_var, width=12, style="Input.TEntry"
        )
        self.port_entry.grid(row=0, column=5, sticky="ew", padx=(0, 8), pady=4)
        self.refresh_button = ttk.Button(connection, text="刷新串口", command=self._refresh_ports)
        self.refresh_button.grid(row=0, column=6, sticky="e", pady=4)

        ttk.Label(connection, text="解析协议").grid(row=1, column=0, sticky="w", padx=(0, 6), pady=(10, 2))
        self.protocol_box = ttk.Combobox(
            connection,
            textvariable=self.protocol_var,
            state="readonly",
            values=(FRAMED_PROTOCOL, RAW_PROTOCOL),
            width=24,
            style="Input.TCombobox",
        )
        self.protocol_box.grid(row=1, column=1, sticky="ew", padx=(0, 14), pady=(10, 2))
        ttk.Label(connection, text="图像颜色格式").grid(row=1, column=2, sticky="w", padx=(0, 6), pady=(10, 2))
        self.pixel_layout_box = ttk.Combobox(
            connection,
            textvariable=self.pixel_layout_var,
            state="readonly",
            values=list(PIXEL_LAYOUTS),
            width=30,
            style="Input.TCombobox",
        )
        self.pixel_layout_box.grid(row=1, column=3, sticky="ew", padx=(0, 14), pady=(10, 2))
        self.save_current_button = ttk.Button(connection, text="保存当前帧", command=self.save_current_frame)
        self.save_current_button.grid(row=1, column=4, sticky="ew", padx=(0, 8), pady=(10, 2))
        self.record_button = ttk.Button(connection, text="开始保存全部帧", command=self.toggle_frame_recording)
        self.record_button.grid(row=1, column=5, sticky="ew", padx=(0, 8), pady=(10, 2))
        self.connect_button = ttk.Button(connection, text="开始接收", style="Accent.TButton", command=self.toggle_connection)
        self.connect_button.grid(row=1, column=6, sticky="e", pady=(10, 2))
        ttk.Label(connection, textvariable=self.storage_info_var, foreground="#94a3b8").grid(
            row=2, column=0, columnspan=6, sticky="w", pady=(8, 0)
        )
        ttk.Button(connection, text="清空日志", command=self.clear_logs).grid(
            row=2, column=6, sticky="e", pady=(8, 0)
        )

        main = ttk.PanedWindow(self.root, orient="horizontal")
        main.grid(row=2, column=0, sticky="nsew", padx=14, pady=(10, 8))

        image_card = ttk.LabelFrame(main, text=" 实时图像 ", padding=(10, 10))
        image_card.columnconfigure(0, weight=1)
        image_card.rowconfigure(1, weight=1)
        ttk.Label(image_card, textvariable=self.image_info_var, foreground="#93c5fd").grid(row=0, column=0, sticky="w", pady=(0, 8))
        self.canvas = tk.Canvas(image_card, background="#06101f", highlightthickness=0)
        self.canvas.grid(row=1, column=0, sticky="nsew")
        self.canvas.bind("<Configure>", self._on_canvas_resize)
        main.add(image_card, weight=3)

        log_card = ttk.LabelFrame(main, text=" 分级调试信息 ", padding=(2, 2))
        log_card.columnconfigure(0, weight=1)
        log_card.rowconfigure(0, weight=1)
        log_split = ttk.PanedWindow(log_card, orient="vertical")
        log_split.grid(row=0, column=0, sticky="nsew")
        self.log_panes = {
            "info": LogPane(log_split, "INFO", "#5eead4"),
            "warning": LogPane(log_split, "WARNING", "#fbbf24"),
            "error": LogPane(log_split, "ERROR", "#fb7185"),
        }
        for level in ("info", "warning", "error"):
            log_split.add(self.log_panes[level], weight=1)
        main.add(log_card, weight=2)

        footer = ttk.Frame(self.root, style="Card.TFrame", padding=(14, 8))
        footer.grid(row=3, column=0, sticky="ew", padx=14, pady=(0, 14))
        ttk.Label(footer, textvariable=self.stats_var, style="Section.TLabel", foreground="#94a3b8").grid(row=0, column=0, sticky="w")

    def _on_transport_change(self) -> None:
        mode = TRANSPORTS[self.transport_var.get()]
        if mode == "serial":
            self.endpoint_label_var.set("串口")
            self.port_label_var.set("波特率")
            self._refresh_ports()
            if self.endpoint_var.get() in ("", "0.0.0.0"):
                self.endpoint_var.set("COM3")
            if self.port_var.get() == "8086":
                self.port_var.set("115200")
            self.refresh_button.configure(state="normal")
        else:
            self.endpoint_label_var.set("监听地址" if mode == "tcp_listener" else "服务器地址")
            self.port_label_var.set("TCP 端口")
            self.endpoint_box.configure(values=())
            if self.endpoint_var.get().upper().startswith("COM") or (
                mode == "tcp_client" and self.endpoint_var.get() == "0.0.0.0"
            ):
                self.endpoint_var.set("0.0.0.0" if mode == "tcp_listener" else "192.168.137.1")
            if self.port_var.get() == "115200":
                self.port_var.set("8086")
            self.refresh_button.configure(state="disabled")

    def _refresh_ports(self) -> None:
        previous = self.endpoint_var.get()
        options = available_serial_ports()
        self._serial_display_to_device = {
            option.display_name: option.device for option in options
        }
        labels = list(self._serial_display_to_device)
        self.endpoint_box.configure(values=labels)
        if previous in self._serial_display_to_device:
            return
        for label, device in self._serial_display_to_device.items():
            if previous == device:
                self.endpoint_var.set(label)
                return
        if labels:
            self.endpoint_var.set(labels[0])

    def toggle_connection(self) -> None:
        if self.worker is not None:
            self.disconnect()
            return
        transport = TRANSPORTS[self.transport_var.get()]
        endpoint = self.endpoint_var.get().strip()
        if transport == "serial":
            endpoint = self._serial_display_to_device.get(endpoint, endpoint)
        try:
            port_or_baud = int(self.port_var.get().strip())
            if not endpoint or not (1 <= port_or_baud <= 65535 if "TCP" in self.transport_var.get() else port_or_baud > 0):
                raise ValueError
        except ValueError:
            messagebox.showerror("输入配置无效", "请填写有效的地址/串口和端口/波特率。")
            return
        self.stream_parser.reset()
        self.raw_parser.reset()
        self._last_sequence = None
        self.lost_packet_count = 0
        self._frame_times.clear()
        self.frame_rate = 0.0
        self.worker = InputWorker(transport, endpoint, port_or_baud, self.events)
        self.worker.start()
        self.connect_button.configure(text="停止接收")
        self._set_status("正在连接…", connected=False)

    def disconnect(self) -> None:
        if self.worker is not None:
            self.worker.request_stop()
            self.worker.join(timeout=1.2)
            self.worker = None
        self.connect_button.configure(text="开始接收")
        self._set_status("已停止", connected=False)

    def _drain_events(self) -> None:
        try:
            while True:
                kind, data = self.events.get_nowait()
                if kind == "data":
                    self._consume_bytes(data if isinstance(data, bytes) else b"")
                elif kind == "status":
                    self._set_status(str(data), connected=True)
                elif kind == "error":
                    self._set_status(f"连接错误：{data}", connected=False)
                    self._append_system_error(str(data))
                elif kind == "stopped" and self.worker is not None and not self.worker.is_alive():
                    self.worker = None
                    self.connect_button.configure(text="开始接收")
                    self._set_status(str(data), connected=False)
        except queue.Empty:
            pass
        if self._preview_dirty:
            self._preview_dirty = False
            self._render_current_image()
        self._update_stats()
        self.root.after(30, self._drain_events)

    def _consume_bytes(self, data: bytes) -> None:
        if self.protocol_var.get() == RAW_PROTOCOL:
            for message in self.raw_parser.feed(data):
                self._show_message(message)
            return
        for packet in self.stream_parser.feed(data):
            self.packet_count += 1
            self._track_sequence(packet.sequence)
            if packet.packet_type == PACKET_IMAGE_RGB565:
                self._show_image(packet)
            elif packet.packet_type == PACKET_TEXT:
                for message in parse_text_payload(packet.payload):
                    self._show_message(message)

    def _track_sequence(self, sequence: int) -> None:
        if self._last_sequence is not None:
            expected = (self._last_sequence + 1) & 0xFFFF
            gap = (sequence - expected) & 0xFFFF
            # A large backward jump is a device reset, rather than 65k lost
            # packets.  Forward gaps are reported in the footer statistics.
            if 0 < gap < 0x8000:
                self.lost_packet_count += gap
        self._last_sequence = sequence

    def _show_message(self, message) -> None:
        stamp = message.received_at.strftime("%H:%M:%S.%f")[:-3]
        line = f"[{stamp}] {message.description}"
        if message.data_text:
            line += f"  |  数据: {message.data_text}"
        self.log_panes.get(message.level, self.log_panes["info"]).append(line)
        self.log_count += 1

    def _show_image(self, packet: Packet) -> None:
        now = perf_counter()
        self._frame_times.append(now)
        while self._frame_times and self._frame_times[0] < now - 1.0:
            self._frame_times.popleft()
        self.frame_rate = float(len(self._frame_times))
        self.image_count += 1
        self.last_image_packet = packet
        if self.frame_saver.is_recording:
            self.frame_saver.enqueue_recorded(packet, self._selected_pixel_layout())
        # Rendering only the newest received image during a GUI cycle keeps
        # latency stable even when the camera sends faster than Tk can paint.
        self._preview_dirty = True

    def _selected_pixel_layout(self) -> str:
        return PIXEL_LAYOUTS.get(self.pixel_layout_var.get(), PIXEL_LAYOUT_RGB565_MSB)

    def _on_pixel_layout_change(self, *_unused: object) -> None:
        if self.last_image_packet is not None:
            self._render_current_image()

    def _render_current_image(self) -> None:
        packet = self.last_image_packet
        if packet is None:
            return
        try:
            layout = self._selected_pixel_layout()
            source_key = (id(packet.payload), layout, packet.flags)
            if self._source_rgb_key != source_key:
                self._source_rgb = rgb565_to_rgb888(
                    packet.payload,
                    packet.width,
                    packet.height,
                    layout=layout,
                    packet_flags=packet.flags,
                )
                self._source_rgb_key = source_key
            target_width, target_height = fit_size(
                packet.width,
                packet.height,
                max(1, self.canvas.winfo_width()),
                max(1, self.canvas.winfo_height()),
            )
            display_rgb = resize_rgb888_nearest(
                self._source_rgb,
                packet.width,
                packet.height,
                target_width,
                target_height,
            )
            self.photo = tk.PhotoImage(
                data=rgb888_to_ppm(display_rgb, target_width, target_height),
                format="PPM",
            )
            if self.image_item is None:
                self.image_item = self.canvas.create_image(0, 0, image=self.photo, anchor="nw")
            else:
                self.canvas.itemconfigure(self.image_item, image=self.photo)
            self._center_image()
            save_state = " · 正在录制" if self.frame_saver.is_recording else ""
            self.image_info_var.set(
                f"RGB565 · {packet.width} × {packet.height} · {self.frame_rate:.1f} FPS · "
                f"序号 {packet.sequence} · 第 {self.image_count} 帧{save_state}"
            )
        except (tk.TclError, ValueError) as exc:
            self._append_system_error(f"图像显示失败：{exc}")

    def save_current_frame(self) -> None:
        if self.last_image_packet is None:
            messagebox.showinfo("尚无图像", "接收到第一帧图像后才能保存。")
            return
        path = self.frame_saver.enqueue_current(
            self.last_image_packet, self._selected_pixel_layout()
        )
        if path is None:
            self.storage_info_var.set("保存队列已满，当前帧未保存")
            return
        self.storage_info_var.set(f"当前帧已加入保存队列：{path.name}")

    def toggle_frame_recording(self) -> None:
        if self.frame_saver.is_recording:
            directory = self.frame_saver.stop_recording()
            self.record_button.configure(text="开始保存全部帧")
            self.storage_info_var.set(
                f"已停止接收新保存任务；已有帧会写入 {directory}" if directory else "已停止保存"
            )
            return
        directory = self.frame_saver.start_recording()
        self.record_button.configure(text="结束保存全部帧")
        self.storage_info_var.set(f"正在保存全部接收帧：{directory}")

    def _center_image(self) -> None:
        if self.photo is None or self.image_item is None:
            return
        x = max(0, (self.canvas.winfo_width() - self.photo.width()) // 2)
        y = max(0, (self.canvas.winfo_height() - self.photo.height()) // 2)
        self.canvas.coords(self.image_item, x, y)

    def _on_canvas_resize(self, _event: tk.Event) -> None:
        """Debounce resize events, then keep the preview fitted proportionally."""

        if self.last_image_packet is None or self._resize_render_pending:
            return
        self._resize_render_pending = True
        self.root.after(80, self._render_after_resize)

    def _render_after_resize(self) -> None:
        self._resize_render_pending = False
        self._render_current_image()

    def _append_system_error(self, text: str) -> None:
        from protocol import DebugMessage
        from datetime import datetime

        self._show_message(DebugMessage("error", "接收器", (), "", datetime.now(), text))

    def _set_status(self, text: str, connected: bool) -> None:
        self.status_var.set(text)
        self.status_label.configure(style="Connected.TLabel" if connected else "Disconnected.TLabel")

    def _update_stats(self) -> None:
        saved, dropped, save_errors, _last_error = self.frame_saver.stats()
        self.stats_var.set(
            f"数据包 {self.packet_count} · 图像 {self.image_count} · FPS {self.frame_rate:.1f} · "
            f"日志 {self.log_count} · 丢包 {self.lost_packet_count} · 丢弃字节 {self.stream_parser.dropped_bytes} · "
            f"头校验错误 {self.stream_parser.header_crc_errors} · 载荷校验错误 {self.stream_parser.payload_crc_errors} · "
            f"已保存 {saved} · 保存跳过 {dropped} · 保存错误 {save_errors}"
        )

    def clear_logs(self) -> None:
        for pane in self.log_panes.values():
            pane.clear()

    def close(self) -> None:
        self.disconnect()
        self.frame_saver.close()
        self.root.destroy()


def main() -> None:
    root = tk.Tk()
    ImageTransmissionApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
