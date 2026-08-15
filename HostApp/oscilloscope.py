"""Resizable multi-channel oscilloscope for validated Printf numeric data."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from datetime import datetime
from bisect import bisect_left
import math
import tkinter as tk
from tkinter import ttk

from protocol import DebugMessage


@dataclass(frozen=True)
class ScopeSample:
    timestamp: float
    values: tuple[float, ...]


class ScopeModel:
    """Bounded, testable time-series storage independent from Tk widgets."""

    def __init__(self, history_seconds: float = 30.0, max_samples: int = 6000) -> None:
        self.history_seconds = history_seconds
        self.samples: deque[ScopeSample] = deque(maxlen=max_samples)
        self.enabled = False
        self.time_window_seconds = 10.0
        self._manual_y_limits: tuple[float, float] | None = None

    def add_message(self, message: DebugMessage) -> bool:
        if not self.enabled or not message.values:
            return False
        if not all(math.isfinite(value) for value in message.values):
            return False
        sample = ScopeSample(message.received_at.timestamp(), message.values)
        self.samples.append(sample)
        cutoff = sample.timestamp - self.history_seconds
        while self.samples and self.samples[0].timestamp < cutoff:
            self.samples.popleft()
        return True

    def clear(self) -> None:
        self.samples.clear()
        self._manual_y_limits = None

    def visible_samples(self) -> tuple[float, list[ScopeSample]]:
        if not self.samples:
            return 0.0, []
        end_time = self.samples[-1].timestamp
        start_time = end_time - self.time_window_seconds
        return end_time, [sample for sample in self.samples if sample.timestamp >= start_time]

    def y_limits(self, visible: list[ScopeSample]) -> tuple[float, float]:
        if self._manual_y_limits is not None:
            return self._manual_y_limits
        values = [value for sample in visible for value in sample.values]
        if not values:
            return -1.0, 1.0
        lower, upper = min(values), max(values)
        if lower == upper:
            padding = max(abs(lower) * 0.1, 1.0)
        else:
            padding = (upper - lower) * 0.1
        return lower - padding, upper + padding

    def zoom(self, factor: float, visible: list[ScopeSample]) -> None:
        """factor < 1 zooms in; factor > 1 zooms out on both axes."""

        self.time_window_seconds = min(
            self.history_seconds, max(0.1, self.time_window_seconds * factor)
        )
        lower, upper = self.y_limits(visible)
        center = (lower + upper) / 2.0
        half_range = max((upper - lower) * factor / 2.0, 1e-9)
        self._manual_y_limits = (center - half_range, center + half_range)

    def auto_scale(self) -> None:
        self._manual_y_limits = None

    @property
    def automatic_y_scale(self) -> bool:
        return self._manual_y_limits is None


class OscilloscopePane(ttk.Frame):
    """Canvas chart with capture enable, zoom, autoscale, and clear controls."""

    _COLORS = (
        "#38bdf8", "#fbbf24", "#fb7185", "#a78bfa",
        "#34d399", "#fb923c", "#e879f9", "#94a3b8",
    )

    def __init__(self, parent: tk.Misc) -> None:
        super().__init__(parent, padding=(10, 8))
        self.model = ScopeModel()
        self.enabled_var = tk.BooleanVar(value=False)
        self.view_var = tk.StringVar(value="关闭；开启后捕获通过校验的数值日志")
        self._redraw_pending = False
        self._plot_state: tuple[
            int, int, int, int, float, float, float, list[ScopeSample]
        ] | None = None

        self.columnconfigure(0, weight=1)
        self.rowconfigure(1, weight=1)
        controls = ttk.Frame(self, style="Card.TFrame")
        controls.grid(row=0, column=0, sticky="ew", pady=(0, 7))
        controls.columnconfigure(5, weight=1)
        ttk.Checkbutton(
            controls,
            text="启用示波器",
            variable=self.enabled_var,
            command=self._on_enabled_changed,
        ).grid(row=0, column=0, sticky="w", padx=(4, 12), pady=3)
        ttk.Button(controls, text="放大", command=lambda: self._zoom(1 / 1.6)).grid(
            row=0, column=1, padx=3, pady=3
        )
        ttk.Button(controls, text="缩小", command=lambda: self._zoom(1.6)).grid(
            row=0, column=2, padx=3, pady=3
        )
        ttk.Button(controls, text="自动缩放", command=self._auto_scale).grid(
            row=0, column=3, padx=3, pady=3
        )
        ttk.Button(controls, text="清除", command=self._clear).grid(
            row=0, column=4, padx=3, pady=3
        )
        ttk.Label(controls, textvariable=self.view_var, style="Section.TLabel", foreground="#94a3b8").grid(
            row=0, column=5, sticky="e", padx=(10, 4)
        )

        self.canvas = tk.Canvas(
            self, background="#06101f", highlightthickness=0, cursor="crosshair"
        )
        self.canvas.grid(row=1, column=0, sticky="nsew")
        self.canvas.bind("<Configure>", lambda _event: self.request_redraw())
        self.canvas.bind("<Motion>", self._on_pointer_motion)
        self.canvas.bind("<Leave>", lambda _event: self._hide_tooltip())
        self.canvas.bind("<MouseWheel>", self._on_mouse_wheel)
        self.canvas.bind("<Button-4>", lambda _event: self._zoom(1 / 1.25))
        self.canvas.bind("<Button-5>", lambda _event: self._zoom(1.25))
        self.request_redraw()

    def add_message(self, message: DebugMessage) -> None:
        if self.model.add_message(message):
            self.request_redraw()

    def request_redraw(self) -> None:
        if self._redraw_pending:
            return
        self._redraw_pending = True
        self.after(33, self._draw)

    def _on_enabled_changed(self) -> None:
        self.model.enabled = self.enabled_var.get()
        self.request_redraw()

    def _zoom(self, factor: float) -> None:
        _end_time, visible = self.model.visible_samples()
        self.model.zoom(factor, visible)
        self.request_redraw()

    def _auto_scale(self) -> None:
        self.model.auto_scale()
        self.request_redraw()

    def _clear(self) -> None:
        self.model.clear()
        self.request_redraw()

    def _draw(self) -> None:
        self._redraw_pending = False
        canvas = self.canvas
        canvas.delete("all")
        width, height = max(1, canvas.winfo_width()), max(1, canvas.winfo_height())
        left, right, top, bottom = 58, 14, 16, 30
        plot_width = max(1, width - left - right)
        plot_height = max(1, height - top - bottom)
        end_time, visible = self.model.visible_samples()
        lower, upper = self.model.y_limits(visible)
        self._plot_state = (left, top, plot_width, plot_height, end_time, lower, upper, visible)

        self._draw_grid(left, top, plot_width, plot_height, lower, upper)
        if not self.model.enabled:
            canvas.create_text(
                width // 2,
                height // 2,
                text="示波器已关闭",
                fill="#94a3b8",
                font=("Microsoft YaHei UI", 11),
            )
        elif not visible:
            canvas.create_text(
                width // 2,
                height // 2,
                text="等待通过校验的数值数据",
                fill="#94a3b8",
                font=("Microsoft YaHei UI", 11),
            )
        else:
            self._draw_series(left, top, plot_width, plot_height, end_time, lower, upper, visible)

        y_mode = "Y 自动" if self.model.automatic_y_scale else "Y 手动"
        self.view_var.set(f"时间窗 {self.model.time_window_seconds:.2g}s · {y_mode} · {len(visible)} 点")

    def _on_mouse_wheel(self, event: tk.Event) -> None:
        """Zoom the current time/amplitude view with the mouse wheel."""

        if event.delta == 0:
            return
        self._zoom(1 / 1.25 if event.delta > 0 else 1.25)

    def _on_pointer_motion(self, event: tk.Event) -> None:
        """Show the nearest plotted sample when the pointer reaches a line."""

        state = self._plot_state
        if state is None:
            self._hide_tooltip()
            return
        left, top, plot_width, plot_height, end_time, lower, upper, visible = state
        if (
            not visible
            or event.x < left
            or event.x > left + plot_width
            or event.y < top
            or event.y > top + plot_height
        ):
            self._hide_tooltip()
            return

        start_time = end_time - self.model.time_window_seconds
        target_time = start_time + (event.x - left) / plot_width * self.model.time_window_seconds
        timestamps = [sample.timestamp for sample in visible]
        right = bisect_left(timestamps, target_time)
        candidates = visible[max(0, right - 1) : min(len(visible), right + 1)]
        if not candidates:
            self._hide_tooltip()
            return
        sample = min(candidates, key=lambda item: abs(item.timestamp - target_time))
        y_range = max(upper - lower, 1e-9)
        distances = [
            abs(event.y - (top + (upper - value) / y_range * plot_height))
            for value in sample.values
        ]
        if not distances or min(distances) > 12:
            self._hide_tooltip()
            return

        relative_time = sample.timestamp - end_time
        channels = "  ".join(
            f"CH{index + 1}={value:.6g}" for index, value in enumerate(sample.values)
        )
        self._show_tooltip(event.x, event.y, f"t={relative_time:.3f}s\n{channels}")

    def _show_tooltip(self, x: int, y: int, text: str) -> None:
        canvas = self.canvas
        self._hide_tooltip()
        label = canvas.create_text(
            x + 10,
            y - 10,
            text=text,
            anchor="sw",
            fill="#e2e8f0",
            font=("Microsoft YaHei UI", 9),
            tags="scope-tooltip",
        )
        bounds = canvas.bbox(label)
        if bounds is None:
            return
        left, top, right, bottom = bounds
        width, height = canvas.winfo_width(), canvas.winfo_height()
        dx = min(0, width - 6 - right) - min(0, left - 6)
        dy = max(0, 6 - top) - max(0, bottom - (height - 6))
        if dx or dy:
            canvas.move(label, dx, dy)
            bounds = canvas.bbox(label)
            if bounds is None:
                return
            left, top, right, bottom = bounds
        background = canvas.create_rectangle(
            left - 6, top - 4, right + 6, bottom + 4,
            fill="#172554", outline="#60a5fa", tags="scope-tooltip"
        )
        canvas.tag_lower(background, label)

    def _hide_tooltip(self) -> None:
        self.canvas.delete("scope-tooltip")

    def _draw_grid(
        self, left: int, top: int, plot_width: int, plot_height: int, lower: float, upper: float
    ) -> None:
        canvas = self.canvas
        grid_color, label_color, axis_color = "#26364f", "#94a3b8", "#64748b"
        for index in range(6):
            fraction = index / 5.0
            x = left + fraction * plot_width
            y = top + (1.0 - fraction) * plot_height
            canvas.create_line(x, top, x, top + plot_height, fill=grid_color)
            canvas.create_line(left, y, left + plot_width, y, fill=grid_color)
            amplitude = lower + fraction * (upper - lower)
            canvas.create_text(left - 7, y, text=f"{amplitude:.4g}", anchor="e", fill=label_color, font=("Microsoft YaHei UI", 8))
            seconds = -self.model.time_window_seconds + fraction * self.model.time_window_seconds
            canvas.create_text(x, top + plot_height + 15, text=f"{seconds:.3g}s", fill=label_color, font=("Microsoft YaHei UI", 8))
        canvas.create_line(left, top, left, top + plot_height, fill=axis_color, width=2)
        canvas.create_line(left, top + plot_height, left + plot_width, top + plot_height, fill=axis_color, width=2)
        canvas.create_text(left + plot_width // 2, top + plot_height + 27, text="时间", fill=label_color, font=("Microsoft YaHei UI", 9))
        canvas.create_text(12, top + plot_height // 2, text="幅值", fill=label_color, font=("Microsoft YaHei UI", 9), angle=90)

    def _draw_series(
        self,
        left: int,
        top: int,
        plot_width: int,
        plot_height: int,
        end_time: float,
        lower: float,
        upper: float,
        visible: list[ScopeSample],
    ) -> None:
        channel_count = max(len(sample.values) for sample in visible)
        lines: list[list[float]] = [[] for _ in range(channel_count)]
        range_size = max(upper - lower, 1e-9)
        start_time = end_time - self.model.time_window_seconds
        for sample in visible:
            x = left + (sample.timestamp - start_time) / self.model.time_window_seconds * plot_width
            for index, value in enumerate(sample.values):
                y = top + (upper - value) / range_size * plot_height
                lines[index].extend((x, y))
        for index, points in enumerate(lines):
            color = self._COLORS[index % len(self._COLORS)]
            if len(points) >= 4:
                self.canvas.create_line(*points, fill=color, width=2, smooth=False)
            elif len(points) == 2:
                self.canvas.create_oval(points[0] - 2, points[1] - 2, points[0] + 2, points[1] + 2, fill=color, outline=color)
            # Wrap the legend instead of letting many channels overlap or run
            # outside a narrow, resizable plot area.
            legend_columns = max(1, plot_width // 58)
            legend_row, legend_column = divmod(index, legend_columns)
            legend_x = left + 8 + legend_column * 58
            legend_y = top + 7 + legend_row * 14
            self.canvas.create_rectangle(
                legend_x, legend_y, legend_x + 10, legend_y + 5, fill=color, outline=color
            )
            self.canvas.create_text(
                legend_x + 14, legend_y + 3, text=f"CH{index + 1}", anchor="w", fill="#dbeafe", font=("Microsoft YaHei UI", 8)
            )
