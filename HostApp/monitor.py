"""Modern, resizable desktop monitor for IMGT image and Printf data streams."""

from __future__ import annotations

from collections import deque
from datetime import datetime
from pathlib import Path
import queue
import threading
import tkinter as tk
from time import perf_counter
from tkinter import filedialog, messagebox, ttk

from frame_saver import FrameSaveWorker
from image_processing import (
    ColourDetectionPane,
    IpmPane,
    ProcessingConfigError,
    load_processing_config,
    save_embedded_processing_config,
    save_processing_config,
)
from oscilloscope import OscilloscopePane
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
import theme as ui


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
            background=ui.PLOT,
            foreground=ui.TEXT,
            insertbackground=ui.TEXT,
            selectbackground=ui.BLUE_DARK,
            font=("Microsoft YaHei UI", 10),
            state="disabled",
        )
        self.text.grid(row=1, column=0, sticky="nsew")
        scrollbar = ttk.Scrollbar(self, orient="vertical", command=self.text.yview)
        scrollbar.grid(row=1, column=1, sticky="ns")
        self.text.configure(yscrollcommand=scrollbar.set)

    def append(self, text: str) -> None:
        self.append_batch([text])

    def append_batch(self, lines: list[str]) -> None:
        """Append a group with one Tk update instead of one update per line."""

        if not lines:
            return
        self.text.configure(state="normal")
        self.text.insert("end", "\n".join(lines) + "\n")
        # Keep both memory use and text-widget reflow bounded in long-running
        # telemetry sessions.  Retain the newest 1,000 rendered lines.
        line_count = int(self.text.index("end-1c").split(".")[0])
        if line_count > 1200:
            self.text.delete("1.0", f"{line_count - 999}.0")
        self.text.see("end")
        self.text.configure(state="disabled")

    def clear(self) -> None:
        self.text.configure(state="normal")
        self.text.delete("1.0", "end")
        self.text.configure(state="disabled")


class ImageTransmissionApp:
    def __init__(self, root: tk.Tk, processing_state_path: Path | None = None) -> None:
        self.root = root
        self.processing_state_path = (Path(processing_state_path) if processing_state_path is not None
                                      else Path(__file__).resolve().parent / "last_processing_config.json")
        self._closing = False
        self._shutdown_complete = threading.Event()
        self._shutdown_error = ""
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
        self._source_payload: bytes | None = None
        self._source_rgb_key: tuple | None = None
        self._live_render_rgb: bytes | None = None
        self._live_render_size: tuple | None = None
        self._last_preview_render_at = 0.0
        self._preview_frame_interval = 1.0 / 30.0
        self._last_stats_update_at = 0.0
        self._last_log_flush_at = 0.0
        self._resize_render_pending = False
        self._preview_dirty = False
        self._processing_refresh_pending = False
        self._pending_log_lines: dict[str, list[str]] = {
            "info": [], "warning": [], "error": []
        }
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
        self._restore_processing_state()
        self.pixel_layout_var.trace_add("write", self._on_pixel_layout_change)
        self._on_transport_change()
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        self.root.after(30, self._drain_events)

    def _configure_style(self) -> None:
        style = ttk.Style(self.root)
        style.theme_use("clam")
        self.root.configure(background=ui.CANVAS)
        body_font = ("Microsoft YaHei UI", 10)
        action_font = ("Microsoft YaHei UI", 10, "bold")
        style.configure("TFrame", background=ui.SURFACE)
        style.configure("Card.TFrame", background=ui.SURFACE)
        style.configure("Header.TFrame", background=ui.CANVAS)
        style.configure("Footer.TFrame", background=ui.CANVAS)
        style.configure(
            "TLabelframe", background=ui.SURFACE, foreground=ui.TEXT,
            bordercolor=ui.HAIRLINE, lightcolor=ui.HAIRLINE,
            darkcolor=ui.HAIRLINE, borderwidth=1, relief="solid",
        )
        style.configure(
            "TLabelframe.Label", background=ui.SURFACE, foreground=ui.TEXT,
            font=("Microsoft YaHei UI", 11, "bold"), padding=(6, 0),
        )
        style.configure("TLabel", background=ui.SURFACE, foreground=ui.TEXT, font=body_font)
        style.configure("Section.TLabel", background=ui.SURFACE, foreground=ui.TEXT, font=action_font)
        style.configure("Header.TLabel", background=ui.CANVAS, foreground=ui.TEXT, font=body_font)
        style.configure("Footer.TLabel", background=ui.CANVAS, foreground=ui.MUTED, font=("Microsoft YaHei UI", 9))
        style.configure(
            "TButton", background=ui.SURFACE_RAISED, foreground=ui.TEXT,
            bordercolor=ui.HAIRLINE, lightcolor=ui.HAIRLINE,
            darkcolor=ui.HAIRLINE, relief="flat", borderwidth=1,
            padding=(10, 5), font=action_font,
        )
        style.map(
            "TButton", background=[("pressed", ui.FIELD), ("active", ui.FIELD), ("disabled", ui.SURFACE)],
            foreground=[("disabled", ui.FAINT)], bordercolor=[("focus", ui.BLUE), ("active", ui.MUTED)],
        )
        style.configure("Compact.TButton", padding=(6, 5), font=("Microsoft YaHei UI", 9, "bold"))
        style.configure(
            "Accent.TButton", background=ui.BLUE, foreground=ui.TEXT,
            bordercolor=ui.BLUE, lightcolor=ui.BLUE, darkcolor=ui.BLUE,
        )
        style.map(
            "Accent.TButton", background=[("pressed", ui.BLUE_DARK), ("active", ui.BLUE_HOVER), ("disabled", ui.SURFACE_RAISED)],
            foreground=[("disabled", ui.FAINT)], bordercolor=[("focus", ui.TEXT), ("active", ui.BLUE_HOVER)],
        )
        style.configure(
            "Input.TEntry",
            fieldbackground=ui.FIELD, background=ui.FIELD, foreground=ui.TEXT,
            insertcolor=ui.TEXT, bordercolor=ui.HAIRLINE, lightcolor=ui.HAIRLINE,
            darkcolor=ui.HAIRLINE, padding=6, font=body_font,
        )
        style.configure(
            "Input.TSpinbox",
            fieldbackground=ui.FIELD, background=ui.FIELD, foreground=ui.TEXT,
            arrowcolor=ui.MUTED, bordercolor=ui.HAIRLINE,
            lightcolor=ui.HAIRLINE, darkcolor=ui.HAIRLINE, padding=4,
        )
        style.map(
            "Input.TSpinbox",
            fieldbackground=[("readonly", ui.FIELD), ("disabled", ui.SURFACE_RAISED)],
            foreground=[("disabled", ui.FAINT)],
        )
        style.configure(
            "Input.TCombobox",
            fieldbackground=ui.FIELD, background=ui.FIELD, foreground=ui.TEXT,
            arrowcolor=ui.MUTED, bordercolor=ui.HAIRLINE,
            lightcolor=ui.HAIRLINE, darkcolor=ui.HAIRLINE, padding=5,
        )
        style.map(
            "Input.TCombobox",
            fieldbackground=[("readonly", ui.FIELD), ("disabled", ui.SURFACE_RAISED)],
            background=[("readonly", ui.FIELD), ("active", ui.SURFACE_RAISED)],
            foreground=[("readonly", ui.TEXT), ("disabled", ui.FAINT)],
            selectbackground=[("readonly", ui.BLUE)], selectforeground=[("readonly", ui.TEXT)],
        )
        self.root.option_add("*TCombobox*Listbox.background", ui.FIELD)
        self.root.option_add("*TCombobox*Listbox.foreground", ui.TEXT)
        self.root.option_add("*TCombobox*Listbox.selectBackground", ui.BLUE)
        self.root.option_add("*TCombobox*Listbox.selectForeground", ui.TEXT)
        style.configure("TNotebook", background=ui.SURFACE, borderwidth=0, tabmargins=(0, 4, 0, 0))
        style.configure(
            "TNotebook.Tab",
            background=ui.SURFACE,
            foreground=ui.MUTED,
            padding=(15, 8),
            font=("Microsoft YaHei UI", 10, "bold"),
        )
        style.map(
            "TNotebook.Tab",
            background=[("selected", ui.SURFACE_RAISED), ("active", ui.FIELD)],
            foreground=[("selected", ui.TEXT), ("active", ui.TEXT)],
        )
        style.configure("TCheckbutton", background=ui.SURFACE, foreground=ui.TEXT, font=body_font)
        style.map(
            "TCheckbutton",
            background=[("active", ui.SURFACE)], foreground=[("disabled", ui.FAINT)],
        )
        style.configure("Connected.TLabel", background=ui.CANVAS, foreground=ui.SUCCESS, font=action_font)
        style.configure("Disconnected.TLabel", background=ui.CANVAS, foreground=ui.WARNING, font=action_font)
        style.configure("TScale", background=ui.SURFACE, troughcolor=ui.FIELD, bordercolor=ui.SURFACE)
        style.configure("TPanedwindow", background=ui.CANVAS)
        style.configure("Sash", sashthickness=6, background=ui.CANVAS)
        style.configure("Vertical.TScrollbar", background=ui.SURFACE_RAISED, troughcolor=ui.SURFACE, arrowcolor=ui.MUTED)

    def _build_layout(self) -> None:
        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(2, weight=1)

        header = ttk.Frame(self.root, style="Header.TFrame", padding=(6, 7))
        header.grid(row=0, column=0, sticky="ew", padx=16, pady=(8, 5))
        header.columnconfigure(1, weight=1)
        ttk.Label(
            header, text="IMGT", style="Header.TLabel",
            font=("Bahnschrift", 21, "bold"), foreground=ui.TEXT,
        ).grid(row=0, column=0, sticky="w")
        ttk.Label(
            header, text="图像传输与 Printf 调试监视器", style="Header.TLabel",
            foreground=ui.MUTED,
        ).grid(row=0, column=1, sticky="w", padx=(16, 0))
        self.status_label = ttk.Label(header, textvariable=self.status_var, style="Disconnected.TLabel")
        self.status_label.grid(row=0, column=2, sticky="e")

        connection = ttk.LabelFrame(self.root, text=" 输入配置 ", padding=(12, 8))
        connection.grid(row=1, column=0, sticky="ew", padx=14)
        connection.columnconfigure(0, weight=1)

        source_row = ttk.Frame(connection, style="Card.TFrame")
        source_row.grid(row=0, column=0, sticky="ew")
        source_row.columnconfigure(1, weight=2)
        source_row.columnconfigure(3, weight=2)
        source_row.columnconfigure(5, weight=1)
        ttk.Label(source_row, text="输入方式").grid(row=0, column=0, sticky="w", padx=(0, 6), pady=4)
        self.transport_box = ttk.Combobox(
            source_row,
            textvariable=self.transport_var,
            state="readonly",
            values=list(TRANSPORTS),
            width=22,
            style="Input.TCombobox",
        )
        self.transport_box.grid(row=0, column=1, sticky="ew", padx=(0, 12), pady=2)
        self.transport_box.bind("<<ComboboxSelected>>", lambda _event: self._on_transport_change())
        ttk.Label(source_row, textvariable=self.endpoint_label_var).grid(row=0, column=2, sticky="w", padx=(0, 6), pady=4)
        self.endpoint_box = ttk.Combobox(
            source_row, textvariable=self.endpoint_var, width=20, style="Input.TCombobox"
        )
        self.endpoint_box.grid(row=0, column=3, sticky="ew", padx=(0, 12), pady=2)
        ttk.Label(source_row, textvariable=self.port_label_var).grid(row=0, column=4, sticky="w", padx=(0, 6), pady=4)
        self.port_entry = ttk.Entry(
            source_row, textvariable=self.port_var, width=9, style="Input.TEntry"
        )
        self.port_entry.grid(row=0, column=5, sticky="ew", padx=(0, 10), pady=2)
        self.refresh_button = ttk.Button(source_row, text="刷新串口", command=self._refresh_ports)
        self.refresh_button.grid(row=0, column=6, sticky="e", padx=(0, 8), pady=2)
        self.connect_button = ttk.Button(source_row, text="开始接收", style="Accent.TButton", command=self.toggle_connection)
        self.connect_button.grid(row=0, column=7, sticky="e", pady=2)

        format_row = ttk.Frame(connection, style="Card.TFrame")
        format_row.grid(row=1, column=0, sticky="ew", pady=(4, 0))
        format_row.columnconfigure(1, weight=1)
        format_row.columnconfigure(3, weight=2)
        ttk.Label(format_row, text="解析协议").grid(row=0, column=0, sticky="w", padx=(0, 6), pady=4)
        self.protocol_box = ttk.Combobox(
            format_row,
            textvariable=self.protocol_var,
            state="readonly",
            values=(FRAMED_PROTOCOL, RAW_PROTOCOL),
            width=22,
            style="Input.TCombobox",
        )
        self.protocol_box.grid(row=0, column=1, sticky="ew", padx=(0, 12), pady=2)
        ttk.Label(format_row, text="图像颜色格式").grid(row=0, column=2, sticky="w", padx=(0, 6), pady=4)
        self.pixel_layout_box = ttk.Combobox(
            format_row,
            textvariable=self.pixel_layout_var,
            state="readonly",
            values=list(PIXEL_LAYOUTS),
            width=28,
            style="Input.TCombobox",
        )
        self.pixel_layout_box.grid(row=0, column=3, sticky="ew", padx=(0, 12), pady=2)
        self.save_current_button = ttk.Button(format_row, text="保存当前帧", command=self.save_current_frame)
        self.save_current_button.grid(row=0, column=4, sticky="ew", padx=(0, 8), pady=2)
        self.record_button = ttk.Button(format_row, text="开始保存全部帧", command=self.toggle_frame_recording)
        self.record_button.grid(row=0, column=5, sticky="ew", pady=2)

        utility_row = ttk.Frame(connection, style="Card.TFrame")
        utility_row.grid(row=2, column=0, sticky="ew", pady=(5, 0))
        utility_row.columnconfigure(0, weight=1)
        self.storage_info_label = ttk.Label(utility_row, textvariable=self.storage_info_var, foreground=ui.MUTED)
        self.storage_info_label.grid(
            row=0, column=0, sticky="w", padx=(2, 8)
        )
        utility_row.bind("<Configure>", lambda event: self.storage_info_label.configure(wraplength=max(200, event.width - 205)))
        ttk.Button(utility_row, text="清空日志", command=self.clear_logs).grid(
            row=0, column=1, sticky="e", padx=(0, 8)
        )
        self.exit_button = ttk.Button(utility_row, text="退出", command=self.close)
        self.exit_button.grid(row=0, column=2, sticky="e")

        main = ttk.PanedWindow(self.root, orient="horizontal")
        main.grid(row=2, column=0, sticky="nsew", padx=14, pady=(8, 6))

        image_card = ttk.LabelFrame(main, text=" 实时图像 ", padding=(10, 8))
        image_card.columnconfigure(0, weight=1)
        image_card.rowconfigure(1, weight=1)
        image_header = ttk.Frame(image_card, style="Card.TFrame")
        image_header.grid(row=0, column=0, sticky="ew", pady=(0, 7))
        image_header.columnconfigure(0, weight=1)
        self.image_info_label = ttk.Label(image_header, textvariable=self.image_info_var, foreground=ui.MUTED)
        self.image_info_label.grid(row=0, column=0, columnspan=3, sticky="w", pady=(0, 3))
        image_header.bind("<Configure>", lambda event: self.image_info_label.configure(wraplength=max(230, event.width - 8)))
        image_actions = ttk.Frame(image_header, style="Card.TFrame")
        image_actions.grid(row=1, column=0, columnspan=3, sticky="w")
        ttk.Button(image_actions, text="导入处理配置", style="Compact.TButton", command=self.import_processing_config).grid(
            row=0, column=0, padx=(0, 6)
        )
        ttk.Button(image_actions, text="导出配置（JSON + C）", style="Compact.TButton", command=self.export_processing_config).grid(
            row=0, column=1
        )
        self.image_notebook = ttk.Notebook(image_card)
        self.image_notebook.grid(row=1, column=0, sticky="nsew")
        live_tab = ttk.Frame(self.image_notebook, style="Card.TFrame")
        live_tab.columnconfigure(0, weight=1)
        live_tab.rowconfigure(0, weight=1)
        self.canvas = tk.Canvas(live_tab, background=ui.PLOT, highlightthickness=0)
        self.canvas.grid(row=0, column=0, sticky="nsew")
        self.canvas.bind("<Configure>", self._on_canvas_resize)
        self.image_notebook.add(live_tab, text="实时图像")
        self.ipm_pane = IpmPane(self.image_notebook, self._request_processing_refresh)
        self.image_notebook.add(self.ipm_pane, text="逆透视")
        self.colour_pane = ColourDetectionPane(self.image_notebook, self._request_processing_refresh)
        self.image_notebook.add(self.colour_pane, text="颜色检测")
        self.image_notebook.bind("<<NotebookTabChanged>>", self._on_image_tab_changed)
        main.add(image_card, weight=3)

        log_card = ttk.LabelFrame(main, text=" 分级调试信息 ", padding=(7, 7))
        log_card.columnconfigure(0, weight=1)
        log_card.rowconfigure(0, weight=1)
        self.debug_notebook = ttk.Notebook(log_card)
        self.debug_notebook.grid(row=0, column=0, sticky="nsew")

        log_tab = ttk.Frame(self.debug_notebook, style="Card.TFrame")
        log_tab.columnconfigure(0, weight=1)
        log_tab.rowconfigure(0, weight=1)
        log_split = ttk.PanedWindow(log_tab, orient="vertical")
        log_split.grid(row=0, column=0, sticky="nsew")
        self.log_panes = {
            "info": LogPane(log_split, "INFO", ui.SUCCESS),
            "warning": LogPane(log_split, "WARNING", ui.WARNING),
            "error": LogPane(log_split, "ERROR", ui.ERROR),
        }
        for level in ("info", "warning", "error"):
            log_split.add(self.log_panes[level], weight=1)
        self.debug_notebook.add(log_tab, text="分级日志")
        self.scope = OscilloscopePane(self.debug_notebook)
        self.debug_notebook.add(self.scope, text="示波器")
        self.debug_notebook.bind("<<NotebookTabChanged>>", self._on_debug_tab_changed)
        main.add(log_card, weight=2)

        footer = ttk.Frame(self.root, style="Footer.TFrame", padding=(3, 5))
        footer.grid(row=3, column=0, sticky="ew", padx=16, pady=(0, 7))
        self.stats_label = ttk.Label(footer, textvariable=self.stats_var, style="Footer.TLabel")
        self.stats_label.grid(row=0, column=0, sticky="w")
        footer.bind("<Configure>", lambda event: self.stats_label.configure(wraplength=max(400, event.width - 8)))

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
        if self._closing:
            return
        # Do not let a continuous TCP sender monopolise Tk's event loop.  A
        # short budget preserves input throughput while leaving time to paint,
        # resize, hover the scope, and handle button clicks.
        deadline = perf_counter() + 0.010
        processed = 0
        while processed < 256 and perf_counter() < deadline:
            try:
                kind, data = self.events.get_nowait()
            except queue.Empty:
                break
            processed += 1
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
        now = perf_counter()
        if self._preview_dirty and now - self._last_preview_render_at >= self._preview_frame_interval:
            self._preview_dirty = False
            self._last_preview_render_at = now
            self._render_current_image()
        if now - self._last_log_flush_at >= 0.08:
            self._last_log_flush_at = now
            self._flush_log_batches()
        if now - self._last_stats_update_at >= 0.25:
            self._last_stats_update_at = now
            self._update_stats()
        self.root.after(1 if not self.events.empty() else 30, self._drain_events)

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
        level = message.level if message.level in self._pending_log_lines else "info"
        self._pending_log_lines[level].append(line)
        # Packets reaching this point have passed both IMGT CRC checks.  The
        # raw printf compatibility mode has no packet envelope to validate.
        self.scope.add_message(message)
        self.log_count += 1

    def _flush_log_batches(self) -> None:
        """Render queued per-level logs once at the end of a GUI tick."""

        for level, lines in self._pending_log_lines.items():
            if lines:
                self.log_panes[level].append_batch(lines)
                lines.clear()

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
        if self._closing:
            return
        packet = self.last_image_packet
        if packet is None:
            return
        try:
            layout = self._selected_pixel_layout()
            source_key = (packet.width, packet.height, layout, packet.flags)
            if self._source_payload is not packet.payload or self._source_rgb_key != source_key:
                self._source_rgb = rgb565_to_rgb888(
                    packet.payload,
                    packet.width,
                    packet.height,
                    layout=layout,
                    packet_flags=packet.flags,
                )
                self._source_rgb_key = source_key
                self._source_payload = packet.payload
            # A hidden live-image tab needs neither scaling nor Tk uploads.
            if self.image_notebook.select() == str(self.canvas.master):
                target_width, target_height = fit_size(
                    packet.width, packet.height,
                    max(1, self.canvas.winfo_width()), max(1, self.canvas.winfo_height()),
                )
                size = (packet.width, packet.height, target_width, target_height)
                if self._live_render_rgb is not self._source_rgb or self._live_render_size != size:
                    display_rgb = resize_rgb888_nearest(
                        self._source_rgb, packet.width, packet.height, target_width, target_height,
                    )
                    self.photo = tk.PhotoImage(
                        data=rgb888_to_ppm(display_rgb, target_width, target_height), format="PPM",
                    )
                    if self.image_item is None:
                        self.image_item = self.canvas.create_image(0, 0, image=self.photo, anchor="nw")
                    else:
                        self.canvas.itemconfigure(self.image_item, image=self.photo)
                    self._live_render_rgb, self._live_render_size = self._source_rgb, size
                self._center_image()
            save_state = " · 正在录制" if self.frame_saver.is_recording else ""
            self.image_info_var.set(
                f"RGB565 · {packet.width} × {packet.height} · {self.frame_rate:.1f} FPS · "
                f"序号 {packet.sequence} · 第 {self.image_count} 帧{save_state}"
            )
            self._render_active_processing_tab()
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

    def _on_image_tab_changed(self, _event: tk.Event) -> None:
        self._render_current_image()

    def _on_debug_tab_changed(self, _event: tk.Event) -> None:
        """Redraw the scope only after its notebook page becomes visible."""

        if self.debug_notebook.select() == str(self.scope):
            self.scope.request_redraw()

    def _request_processing_refresh(self) -> None:
        """Debounce slider/spinbox changes before running a PC-side preview."""

        if self._closing or self._processing_refresh_pending:
            return
        self._processing_refresh_pending = True
        self.root.after(60, self._render_requested_processing_tab)

    def _render_requested_processing_tab(self) -> None:
        self._processing_refresh_pending = False
        self._render_current_image()

    def _render_active_processing_tab(self) -> None:
        packet = self.last_image_packet
        if packet is None or not self._source_rgb:
            return
        selected = self.image_notebook.select()
        if selected not in (str(self.ipm_pane), str(self.colour_pane)):
            return
        # The pane handles one running job plus the newest pending frame;
        # every rendered camera update reaches it, including the final frame.
        if selected == str(self.ipm_pane):
            self.ipm_pane.update_frame(self._source_rgb, packet.width, packet.height)
        else:
            self.colour_pane.update_frame(self._source_rgb, packet.width, packet.height)

    def export_processing_config(self) -> None:
        path = filedialog.asksaveasfilename(
            parent=self.root,
            title="导出图像处理配置",
            initialdir=str(Path(__file__).resolve().parent),
            initialfile="imgt_processing_config.json",
            defaultextension=".json",
            filetypes=(("JSON 配置文件", "*.json"), ("所有文件", "*.*")),
        )
        if not path:
            return
        try:
            ipm_config = self.ipm_pane.get_config()
            colour_config = self.colour_pane.get_config()
            save_processing_config(path, ipm_config, colour_config)
            json_path = Path(path)
            copy_header_path = json_path.with_name(f"{json_path.stem}_embedded_config.h")
            embedded_header_path = (
                Path(__file__).resolve().parents[1]
                / "EmbedCode"
                / "TC4ImageProcessingConfig.h"
            )
            save_embedded_processing_config(copy_header_path, ipm_config, colour_config)
            save_embedded_processing_config(embedded_header_path, ipm_config, colour_config)
        except (OSError, ValueError, tk.TclError) as error:
            messagebox.showerror("导出失败", str(error), parent=self.root)
            return
        self.storage_info_var.set(
            f"已导出 JSON 与 C 配置：{Path(path).name}；已同步 EmbedCode/TC4ImageProcessingConfig.h"
        )

    def import_processing_config(self) -> None:
        path = filedialog.askopenfilename(
            parent=self.root,
            title="导入图像处理配置",
            initialdir=str(Path(__file__).resolve().parent),
            filetypes=(("JSON 配置文件", "*.json"), ("所有文件", "*.*")),
        )
        if not path:
            return
        try:
            ipm_config, colour_config = load_processing_config(path)
            self.ipm_pane.apply_config(ipm_config)
            self.colour_pane.apply_config(colour_config)
        except (OSError, ValueError, ProcessingConfigError) as error:
            messagebox.showerror("导入失败", str(error), parent=self.root)
            return
        self.storage_info_var.set(f"已导入图像处理配置：{Path(path).name}")
        self._request_processing_refresh()

    def _restore_processing_state(self) -> None:
        """Restore the last session, or an existing export on the first launch."""
        source = self.processing_state_path
        if not source.exists():
            source = Path(__file__).resolve().parent / "imgt_processing_config.json"
        if not source.exists():
            return
        try:
            ipm, colour = load_processing_config(source)
            self.ipm_pane.apply_config(ipm)
            self.colour_pane.apply_config(colour)
        except (OSError, ValueError, tk.TclError) as error:
            self.storage_info_var.set(f"自动恢复配置失败：{source.name}；当前使用默认参数")
            self._append_system_error(f"自动恢复处理参数失败：{source}：{error}")
            return
        self.storage_info_var.set(f"已自动恢复处理参数：{source.name}")

    def _append_system_error(self, text: str) -> None:
        from protocol import DebugMessage
        from datetime import datetime

        self._show_message(DebugMessage("error", f"接收器：{text}", (), "", datetime.now(), text))

    def _set_status(self, text: str, connected: bool) -> None:
        self.status_var.set(text)
        self.status_label.configure(style="Connected.TLabel" if connected else "Disconnected.TLabel")

    def _update_stats(self) -> None:
        saved, dropped, save_errors, _last_error = self.frame_saver.stats()
        self.stats_var.set(
            f"数据包 {self.packet_count} · 图像 {self.image_count} · FPS {self.frame_rate:.1f} · "
            f"日志 {self.log_count} · 丢包 {self.lost_packet_count} · 丢弃字节 {self.stream_parser.dropped_bytes} · "
            f"头校验错误 {self.stream_parser.header_crc_errors} · 载荷校验错误 {self.stream_parser.payload_crc_errors} · "
            f"无 CRC 丢弃 {self.stream_parser.missing_payload_crc_errors} · "
            f"已保存 {saved} · 保存跳过 {dropped} · 保存错误 {save_errors}"
        )

    def clear_logs(self) -> None:
        for pane in self.log_panes.values():
            pane.clear()
        for lines in self._pending_log_lines.values():
            lines.clear()

    def close(self) -> None:
        if self._closing:
            return
        try:
            # Read entry variables directly, including edits that still have
            # keyboard focus. Invalid edits or a disk error keep the app open.
            save_processing_config(self.processing_state_path,
                                   self.ipm_pane.get_config(), self.colour_pane.get_config())
        except (OSError, ValueError, tk.TclError) as error:
            messagebox.showerror("无法保存处理参数", f"参数未保存，软件保持打开。请修正参数或保存路径后再次退出。\n\n{error}",
                                 parent=self.root)
            return
        self._closing = True
        self.root.title("IMGT 图像与调试监视器 · 正在退出")
        self.storage_info_var.set("处理参数已保存，正在完成后台图片保存任务…")
        # Prevent edits after the snapshot was saved while the writer drains.
        def disable_widgets(widget: tk.Misc) -> None:
            if isinstance(widget, ttk.Widget):
                widget.state(["disabled"])
            for child in widget.winfo_children():
                disable_widgets(child)
        disable_widgets(self.root)
        self.ipm_pane.shutdown_processing()
        self.colour_pane.shutdown_processing()
        if self.worker is not None:
            self.worker.request_stop()
        self.frame_saver.stop_recording()
        threading.Thread(target=self._finish_shutdown, name="monitor-shutdown", daemon=False).start()
        self.root.after(30, self._poll_shutdown)

    def _finish_shutdown(self) -> None:
        """Joining I/O threads must never freeze Tk during exit."""
        try:
            if self.worker is not None:
                self.worker.join()
            self.frame_saver.close()
        except Exception as error:
            self._shutdown_error = str(error)
        finally:
            self._shutdown_complete.set()

    def _poll_shutdown(self) -> None:
        if self._shutdown_complete.is_set():
            if self._shutdown_error:
                messagebox.showerror("结束后台任务失败", f"处理参数已保存。\n{self._shutdown_error}", parent=self.root)
            for callback in self.root.tk.call("after", "info"):
                self.root.after_cancel(callback)
            self.root.destroy()
            return
        saved = self.frame_saver.stats()[0]
        self.storage_info_var.set(f"处理参数已保存 · 已保存 {saved} 帧 · 等待后台任务结束…")
        self.root.after(50, self._poll_shutdown)


def main() -> None:
    root = tk.Tk()
    ImageTransmissionApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
