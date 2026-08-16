"""Resizable multi-channel oscilloscope for validated Printf numeric data."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from datetime import datetime
from bisect import bisect_left, bisect_right
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
        self.paused = False
        self.time_window_seconds = 10.0
        self._manual_y_limits: tuple[float, float] | None = None
        # None means the view follows the latest sample.  A timestamp freezes
        # the visible time interval while new samples keep arriving.
        self._view_end_timestamp: float | None = None
        # Drawing and pointer tracking request the same view repeatedly.  Keep
        # a snapshot until data or the time view changes, rather than copying
        # every sample on every 33 ms redraw.
        self._visible_cache_key: tuple[float, float, float, float, int] | None = None
        self._visible_cache: list[ScopeSample] = []
        self._visible_timestamps: list[float] = []
        self._auto_y_cache_for: list[ScopeSample] | None = None
        self._auto_y_cache: tuple[float, float] = (-1.0, 1.0)

    def _invalidate_cached_view(self) -> None:
        self._visible_cache_key = None
        self._auto_y_cache_for = None

    def add_message(self, message: DebugMessage) -> bool:
        if not self.enabled or self.paused or not message.values:
            return False
        if not all(math.isfinite(value) for value in message.values):
            return False
        sample = ScopeSample(message.received_at.timestamp(), message.values)
        self.samples.append(sample)
        cutoff = sample.timestamp - self.history_seconds
        while self.samples and self.samples[0].timestamp < cutoff:
            self.samples.popleft()
        self._invalidate_cached_view()
        return True

    def clear(self) -> None:
        self.samples.clear()
        self._manual_y_limits = None
        self._view_end_timestamp = None
        self._invalidate_cached_view()

    def visible_samples(self) -> tuple[float, list[ScopeSample]]:
        end_time, visible, _timestamps = self.visible_snapshot()
        return end_time, visible

    def visible_snapshot(self) -> tuple[float, list[ScopeSample], list[float]]:
        """Return a cached visible sample range and its timestamps.

        The timestamp list is shared with the view and lets the hover handler
        use binary search without rebuilding it for every mouse movement.
        """

        if not self.samples:
            return 0.0, [], []
        end_time = self.view_end_timestamp()
        start_time = end_time - self.time_window_seconds
        key = (
            self.samples[0].timestamp,
            self.samples[-1].timestamp,
            end_time,
            self.time_window_seconds,
            len(self.samples),
        )
        if key != self._visible_cache_key:
            # deque does not support binary-search indexing efficiently; make
            # one bounded snapshot only when the data/view has actually moved.
            all_samples = list(self.samples)
            timestamps = [sample.timestamp for sample in all_samples]
            first = bisect_left(timestamps, start_time)
            last = bisect_right(timestamps, end_time)
            self._visible_cache = all_samples[first:last]
            self._visible_timestamps = timestamps[first:last]
            self._visible_cache_key = key
            self._auto_y_cache_for = None
        return end_time, self._visible_cache, self._visible_timestamps

    def view_end_timestamp(self) -> float:
        """Return the right edge of the visible time interval, clamped safely."""

        if not self.samples:
            return 0.0
        latest = self.samples[-1].timestamp
        earliest_end = min(latest, self.samples[0].timestamp + self.time_window_seconds)
        if self._view_end_timestamp is None:
            return latest
        clamped = min(latest, max(earliest_end, self._view_end_timestamp))
        if math.isclose(clamped, latest, abs_tol=1.0e-9):
            self._view_end_timestamp = None
            return latest
        self._view_end_timestamp = clamped
        return clamped

    def pan(self, seconds: float) -> None:
        """Move the time view; negative seconds means an earlier history slice."""

        if not self.samples:
            return
        latest = self.samples[-1].timestamp
        earliest_end = min(latest, self.samples[0].timestamp + self.time_window_seconds)
        current = self.view_end_timestamp()
        target = min(latest, max(earliest_end, current + seconds))
        self._view_end_timestamp = None if math.isclose(target, latest, abs_tol=1.0e-9) else target

    def follow_latest(self) -> None:
        self._view_end_timestamp = None

    def time_position(self) -> float:
        """Return 0 (oldest allowed view) through 1 (live view)."""

        if not self.samples:
            return 1.0
        latest = self.samples[-1].timestamp
        earliest_end = min(latest, self.samples[0].timestamp + self.time_window_seconds)
        span = latest - earliest_end
        if span <= 0.0:
            return 1.0
        return (self.view_end_timestamp() - earliest_end) / span

    def set_time_position(self, position: float) -> None:
        if not self.samples:
            return
        latest = self.samples[-1].timestamp
        earliest_end = min(latest, self.samples[0].timestamp + self.time_window_seconds)
        position = min(1.0, max(0.0, position))
        target = earliest_end + (latest - earliest_end) * position
        self._view_end_timestamp = None if position >= 0.999 else target

    def y_limits(self, visible: list[ScopeSample]) -> tuple[float, float]:
        if self._manual_y_limits is not None:
            return self._manual_y_limits
        if self._auto_y_cache_for is visible:
            return self._auto_y_cache
        lower = math.inf
        upper = -math.inf
        for sample in visible:
            for value in sample.values:
                lower = min(lower, value)
                upper = max(upper, value)
        if lower == math.inf:
            self._auto_y_cache_for = visible
            self._auto_y_cache = (-1.0, 1.0)
            return self._auto_y_cache
        if lower == upper:
            padding = max(abs(lower) * 0.1, 1.0)
        else:
            padding = (upper - lower) * 0.1
        self._auto_y_cache_for = visible
        self._auto_y_cache = (lower - padding, upper + padding)
        return self._auto_y_cache

    def zoom_x(self, factor: float) -> None:
        """factor < 1 zooms the time axis in; factor > 1 zooms it out."""
        self.time_window_seconds = min(
            self.history_seconds, max(0.1, self.time_window_seconds * factor)
        )
        self._invalidate_cached_view()

    def zoom_y(self, factor: float, visible: list[ScopeSample]) -> None:
        """factor < 1 zooms the amplitude axis in; factor > 1 zooms it out."""
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
        self.pause_var = tk.StringVar(value="暂停采样")
        self.view_var = tk.StringVar(value="关闭；开启后捕获通过校验的数值日志")
        self.time_position_var = tk.DoubleVar(value=1.0)
        self._redraw_pending = False
        self._updating_time_position = False
        self._drag_last_x: int | None = None
        self._plot_state: tuple[
            int, int, int, int, float, float, float, list[ScopeSample], list[float]
        ] | None = None

        self.columnconfigure(0, weight=1)
        self.rowconfigure(1, weight=1)
        controls = ttk.Frame(self, style="Card.TFrame")
        controls.grid(row=0, column=0, sticky="ew", pady=(0, 7))
        # Keep the frequent controls in two short rows.  The former layout
        # scattered X/Y labels and actions across the full pane, which made
        # the toolbar wrap awkwardly once the main window was narrowed.
        controls.columnconfigure(4, weight=1)
        ttk.Checkbutton(
            controls,
            text="启用示波器",
            variable=self.enabled_var,
            command=self._on_enabled_changed,
        ).grid(row=0, column=0, sticky="w", padx=(5, 8), pady=3)
        self.pause_button = ttk.Button(controls, textvariable=self.pause_var, command=self._toggle_pause)
        self.pause_button.grid(row=0, column=1, padx=3, pady=3)
        ttk.Label(controls, text="水平", style="Section.TLabel").grid(
            row=0, column=2, sticky="e", padx=(10, 1)
        )
        ttk.Button(controls, text="X+", width=4, command=lambda: self._zoom_x(1 / 1.6)).grid(
            row=0, column=3, padx=2, pady=3
        )
        ttk.Button(controls, text="X−", width=4, command=lambda: self._zoom_x(1.6)).grid(
            row=0, column=4, padx=2, pady=3, sticky="w"
        )
        ttk.Label(controls, text="垂直", style="Section.TLabel").grid(
            row=1, column=0, sticky="e", padx=(5, 1)
        )
        ttk.Button(controls, text="Y+", width=4, command=lambda: self._zoom_y(1 / 1.6)).grid(
            row=1, column=1, padx=2, pady=3
        )
        ttk.Button(controls, text="Y−", width=4, command=lambda: self._zoom_y(1.6)).grid(
            row=1, column=2, padx=2, pady=3
        )
        ttk.Button(controls, text="Y自动", command=self._auto_scale).grid(
            row=1, column=3, padx=(7, 2), pady=3
        )
        ttk.Button(controls, text="清除", command=self._clear).grid(
            row=1, column=4, padx=(2, 5), pady=3, sticky="w"
        )
        ttk.Label(controls, text="时间", style="Section.TLabel").grid(
            row=2, column=0, sticky="e", padx=(5, 3)
        )
        ttk.Button(controls, text="◀", command=lambda: self._pan(-1)).grid(
            row=2, column=1, padx=3, pady=3
        )
        self.time_scale = ttk.Scale(
            controls,
            from_=0.0,
            to=1.0,
            variable=self.time_position_var,
            command=self._on_time_position_changed,
        )
        self.time_scale.grid(row=2, column=2, columnspan=3, sticky="ew", padx=3, pady=3)
        ttk.Button(controls, text="▶", command=lambda: self._pan(1)).grid(
            row=2, column=5, padx=3, pady=3
        )
        ttk.Button(controls, text="实时", command=self._follow_latest).grid(
            row=2, column=6, padx=(3, 6), pady=3
        )
        ttk.Label(controls, textvariable=self.view_var, style="Section.TLabel", foreground="#94a3b8").grid(
            row=3, column=0, columnspan=7, sticky="w", padx=(5, 5), pady=(1, 2)
        )

        self.canvas = tk.Canvas(
            self, background="#06101f", highlightthickness=0, cursor="crosshair"
        )
        self.canvas.grid(row=1, column=0, sticky="nsew")
        self.canvas.bind("<Configure>", lambda _event: self.request_redraw())
        self.canvas.bind("<Motion>", self._on_pointer_motion)
        self.canvas.bind("<Leave>", lambda _event: self._hide_tooltip())
        self.canvas.bind("<MouseWheel>", self._on_mouse_wheel)
        self.canvas.bind("<Button-4>", lambda _event: self._zoom_x(1 / 1.25))
        self.canvas.bind("<Button-5>", lambda _event: self._zoom_x(1.25))
        self.canvas.bind("<ButtonPress-1>", self._on_pan_start)
        self.canvas.bind("<B1-Motion>", self._on_pan_drag)
        self.canvas.bind("<ButtonRelease-1>", lambda _event: self._on_pan_end())
        self.bind("<Visibility>", lambda _event: self.request_redraw())
        self._update_pause_button()
        self.request_redraw()

    def add_message(self, message: DebugMessage) -> None:
        if self.model.add_message(message):
            # There is no reason to spend canvas time while the log tab is
            # visible.  The visibility handler redraws immediately on return.
            if self.winfo_ismapped():
                self.request_redraw()

    def request_redraw(self) -> None:
        if self._redraw_pending:
            return
        self._redraw_pending = True
        self.after(33, self._draw)

    def _on_enabled_changed(self) -> None:
        self.model.enabled = self.enabled_var.get()
        if not self.model.enabled:
            self.model.paused = False
        self._update_pause_button()
        self.request_redraw()

    def _toggle_pause(self) -> None:
        if not self.model.enabled:
            return
        self.model.paused = not self.model.paused
        self._update_pause_button()
        self.request_redraw()

    def _update_pause_button(self) -> None:
        self.pause_var.set("继续采样" if self.model.paused else "暂停采样")
        self.pause_button.configure(state="normal" if self.model.enabled else "disabled")

    def _zoom_x(self, factor: float) -> None:
        self.model.zoom_x(factor)
        self.request_redraw()

    def _zoom_y(self, factor: float) -> None:
        _end_time, visible = self.model.visible_samples()
        self.model.zoom_y(factor, visible)
        self.request_redraw()

    def _auto_scale(self) -> None:
        self.model.auto_scale()
        self.request_redraw()

    def _clear(self) -> None:
        self.model.clear()
        self.request_redraw()

    def _pan(self, direction: int) -> None:
        self.model.pan(direction * self.model.time_window_seconds * 0.5)
        self.request_redraw()

    def _follow_latest(self) -> None:
        self.model.follow_latest()
        self.request_redraw()

    def _on_time_position_changed(self, value: str) -> None:
        if self._updating_time_position:
            return
        try:
            self.model.set_time_position(float(value))
        except ValueError:
            return
        self.request_redraw()

    def _on_pan_start(self, event: tk.Event) -> None:
        self._drag_last_x = event.x

    def _on_pan_drag(self, event: tk.Event) -> None:
        if self._drag_last_x is None or self._plot_state is None:
            return
        _left, _top, plot_width, _plot_height, _end, _lower, _upper, _visible, _timestamps = self._plot_state
        if plot_width <= 0:
            return
        seconds = -(event.x - self._drag_last_x) / plot_width * self.model.time_window_seconds
        self._drag_last_x = event.x
        self.model.pan(seconds)
        self.request_redraw()

    def _on_pan_end(self) -> None:
        self._drag_last_x = None

    def _draw(self) -> None:
        self._redraw_pending = False
        canvas = self.canvas
        canvas.delete("all")
        width, height = max(1, canvas.winfo_width()), max(1, canvas.winfo_height())
        left, right, top, bottom = 58, 14, 16, 30
        plot_width = max(1, width - left - right)
        plot_height = max(1, height - top - bottom)
        end_time, visible, timestamps = self.model.visible_snapshot()
        lower, upper = self.model.y_limits(visible)
        self._plot_state = (
            left, top, plot_width, plot_height, end_time, lower, upper, visible, timestamps
        )
        self._updating_time_position = True
        self.time_position_var.set(self.model.time_position())
        self._updating_time_position = False

        self._draw_grid(left, top, plot_width, plot_height, end_time, lower, upper)
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
        capture = "已暂停采样" if self.model.paused else ("采样中" if self.model.enabled else "已关闭")
        latest = self.model.samples[-1].timestamp if self.model.samples else 0.0
        view_mode = "实时" if math.isclose(end_time, latest, abs_tol=1.0e-9) else f"回看 {latest - end_time:.2g}s 前"
        self.view_var.set(
            f"{capture} · {view_mode} · X {self.model.time_window_seconds:.2g}s · {y_mode} · {len(visible)} 点"
        )

    def _on_mouse_wheel(self, event: tk.Event) -> None:
        """Zoom the current time/amplitude view with the mouse wheel."""

        if event.delta == 0:
            return
        if event.state & 0x0001:  # Shift + wheel adjusts amplitude only.
            self._zoom_y(1 / 1.25 if event.delta > 0 else 1.25)
        else:
            self._zoom_x(1 / 1.25 if event.delta > 0 else 1.25)

    def _on_pointer_motion(self, event: tk.Event) -> None:
        """Show the nearest plotted sample when the pointer reaches a line."""

        state = self._plot_state
        if state is None:
            self._hide_tooltip()
            return
        left, top, plot_width, plot_height, end_time, lower, upper, visible, timestamps = state
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
        right = bisect_left(timestamps, target_time)
        # With envelope rendering several samples can share a single screen
        # column.  Check that small time slice so hovering an extremum still
        # reveals the actual sample that produced the visible line.
        time_per_pixel = self.model.time_window_seconds / max(plot_width, 1)
        first = bisect_left(timestamps, target_time - time_per_pixel)
        last = bisect_right(timestamps, target_time + time_per_pixel)
        candidates = visible[first:last]
        if not candidates:
            candidates = visible[max(0, right - 1) : min(len(visible), right + 1)]
        if not candidates:
            self._hide_tooltip()
            return
        y_range = max(upper - lower, 1e-9)
        sample = min(
            candidates,
            key=lambda item: min(
                abs(event.y - (top + (upper - value) / y_range * plot_height))
                for value in item.values
            ),
        )
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
        self,
        left: int,
        top: int,
        plot_width: int,
        plot_height: int,
        end_time: float,
        lower: float,
        upper: float,
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
        # A canvas cannot show more detail than it has horizontal pixels.  At
        # high telemetry rates render an envelope (min/max for each pixel
        # column) instead of handing Tk tens of thousands of vertices.
        if len(visible) > plot_width * 2:
            columns: list[dict[int, list[float]]] = [dict() for _ in range(channel_count)]
            last_column = max(1, plot_width - 1)
            for sample in visible:
                fraction = (sample.timestamp - start_time) / self.model.time_window_seconds
                column = max(0, min(last_column, int(fraction * last_column)))
                for index, value in enumerate(sample.values):
                    y = top + (upper - value) / range_size * plot_height
                    envelope = columns[index].get(column)
                    if envelope is None:
                        columns[index][column] = [y, y]
                    else:
                        envelope[0] = min(envelope[0], y)
                        envelope[1] = max(envelope[1], y)
            for index, channel_columns in enumerate(columns):
                for column, (minimum, maximum) in channel_columns.items():
                    x = left + column
                    lines[index].extend((x, minimum))
                    if maximum - minimum > 0.25:
                        lines[index].extend((x, maximum))
        else:
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
