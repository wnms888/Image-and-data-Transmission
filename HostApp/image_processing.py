"""Live host-side IPM and colour-detection previews.

The calibration and HSL conventions mirror ``EmbedCode/CameraIPM.*`` and
``EmbedCode/ColorDetection.*``.  Processing stays on the PC: it never changes
the received RGB565 frame or the image-saving path.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import math
import tkinter as tk
from tkinter import ttk

from protocol import fit_size, resize_rgb888_nearest, rgb888_to_ppm


# MATLAB calibration copied from CameraIPM.h.  The source calibration image is
# 160 x 128; positions are proportionally adapted when another stream size is
# received so the preview remains useful while making that limitation explicit.
_CALIBRATION_WIDTH = 160
_CALIBRATION_HEIGHT = 128
_FX = 86.0473887
_FY = 88.6329816
_CX = 82.4574101
_CY = 66.3210952
_SKEW = 0.0
_K1 = 0.018489635
_K2 = -0.028478149
_P1 = 0.0
_P2 = 0.0
_H = (
    0.00126378934, 0.176208985, -48.1200764,
    0.447881955, 0.00593926103, -36.8896207,
    0.0173753584, -1.03867922, 1.0,
)


@dataclass(frozen=True)
class IpmConfig:
    """Bird's-eye output range, in the embedded calibration ground units."""

    near_x: float = 0.20
    far_x: float = 2.00
    half_width_y: float = 0.75
    output_width: int = 240
    output_height: int = 240


def _invert_3x3(matrix: tuple[float, ...]) -> tuple[float, ...]:
    a, b, c, d, e, f, g, h, i = matrix
    cofactor = (
        e * i - f * h, c * h - b * i, b * f - c * e,
        f * g - d * i, a * i - c * g, c * d - a * f,
        d * h - e * g, b * g - a * h, a * e - b * d,
    )
    determinant = a * cofactor[0] + b * cofactor[3] + c * cofactor[6]
    if abs(determinant) < 1.0e-12:
        raise ValueError("IPM calibration homography is singular")
    return tuple(value / determinant for value in cofactor)


_H_INV = _invert_3x3(_H)


def _ground_to_raw_pixel(ground_x: float, ground_y: float) -> tuple[float, float] | None:
    """Invert the C pipeline: ground -> undistorted pixel -> raw pixel."""

    w = _H_INV[6] * ground_x + _H_INV[7] * ground_y + _H_INV[8]
    if abs(w) < 1.0e-12:
        return None
    uu = (_H_INV[0] * ground_x + _H_INV[1] * ground_y + _H_INV[2]) / w
    vu = (_H_INV[3] * ground_x + _H_INV[4] * ground_y + _H_INV[5]) / w
    y = (vu - _CY) / _FY
    x = (uu - _CX - _SKEW * y) / _FX
    r2 = x * x + y * y
    radial = 1.0 + _K1 * r2 + _K2 * r2 * r2
    xd = x * radial + 2.0 * _P1 * x * y + _P2 * (r2 + 2.0 * x * x)
    yd = y * radial + _P1 * (r2 + 2.0 * y * y) + 2.0 * _P2 * x * y
    raw_u = _FX * xd + _SKEW * yd + _CX
    raw_v = _FY * yd + _CY
    if not (math.isfinite(raw_u) and math.isfinite(raw_v)):
        return None
    return raw_u, raw_v


class IpmProcessor:
    """Cached reverse lookup from a bird's-eye pixel to the camera image."""

    def __init__(self) -> None:
        self._lookup_key: tuple[int, int, IpmConfig] | None = None
        self._lookup: list[int] = []

    def process(self, rgb: bytes, source_width: int, source_height: int, config: IpmConfig) -> bytes:
        if len(rgb) != source_width * source_height * 3:
            raise ValueError("source RGB888 dimensions do not match data length")
        self._ensure_lookup(source_width, source_height, config)
        output = bytearray(config.output_width * config.output_height * 3)
        destination = 0
        for source in self._lookup:
            if source >= 0:
                output[destination : destination + 3] = rgb[source : source + 3]
            destination += 3
        return bytes(output)

    def _ensure_lookup(self, source_width: int, source_height: int, config: IpmConfig) -> None:
        key = (source_width, source_height, config)
        if key == self._lookup_key:
            return
        if (
            config.output_width < 8
            or config.output_height < 8
            or config.far_x <= config.near_x
            or config.half_width_y <= 0.0
        ):
            raise ValueError("invalid IPM output range")

        lookup: list[int] = []
        x_scale = source_width / _CALIBRATION_WIDTH
        y_scale = source_height / _CALIBRATION_HEIGHT
        for output_y in range(config.output_height):
            x_fraction = output_y / max(1, config.output_height - 1)
            ground_x = config.far_x - x_fraction * (config.far_x - config.near_x)
            for output_x in range(config.output_width):
                y_fraction = output_x / max(1, config.output_width - 1)
                ground_y = config.half_width_y - y_fraction * (2.0 * config.half_width_y)
                source_point = _ground_to_raw_pixel(ground_x, ground_y)
                if source_point is None:
                    lookup.append(-1)
                    continue
                source_u = round(source_point[0] * x_scale)
                source_v = round(source_point[1] * y_scale)
                if 0 <= source_u < source_width and 0 <= source_v < source_height:
                    lookup.append((source_v * source_width + source_u) * 3)
                else:
                    lookup.append(-1)
        self._lookup_key = key
        self._lookup = lookup


@dataclass(frozen=True)
class HslThreshold:
    h_min: int
    h_max: int
    s_min: int
    s_max: int
    l_min: int
    l_max: int

    def matches(self, h: int, s: int, l: int) -> bool:
        hue_matches = (
            self.h_min <= h <= self.h_max
            if self.h_min <= self.h_max
            else h >= self.h_min or h <= self.h_max
        )
        return hue_matches and self.s_min <= s <= self.s_max and self.l_min <= l <= self.l_max


@dataclass(frozen=True)
class ColourDetectionConfig:
    """Defaults copied from g_color_detection_*_threshold in ColorDetection.c."""

    red: HslThreshold = HslThreshold(230, 15, 20, 240, 65, 240)
    yellow: HslThreshold = HslThreshold(18, 42, 80, 240, 70, 240)
    roi_top_percent: int = 25
    minimum_area: int = 10


@dataclass(frozen=True)
class ColourDetectionOutput:
    image: bytes
    red_components: int
    yellow_components: int


def rgb_to_hsl240(red: int, green: int, blue: int) -> tuple[int, int, int]:
    """Match the embedded integer HSL 0..240 conversion convention."""

    maximum = max(red, green, blue)
    minimum = min(red, green, blue)
    difference = maximum - minimum
    lightness = (maximum + minimum) * 120 // 255
    hue = 0
    saturation = 0
    if difference:
        if maximum == red:
            # C integer division truncates toward zero; Python // floors a
            # negative value, so use int() to preserve the embedded rule.
            hue = int(40 * (green - blue) / difference)
            if hue < 0:
                hue += 240
        elif maximum == green:
            hue = 80 + 40 * (blue - red) // difference
        else:
            hue = 160 + 40 * (red - green) // difference
        denominator = maximum + minimum if lightness <= 120 else 510 - (maximum + minimum)
        saturation = difference * 240 // denominator if denominator else 0
    return max(0, min(240, hue)), max(0, min(240, saturation)), max(0, min(240, lightness))


def detect_colours(
    rgb: bytes, width: int, height: int, config: ColourDetectionConfig
) -> ColourDetectionOutput:
    """Apply the embedded red/yellow HSL rules and four-connected area filter."""

    if len(rgb) != width * height * 3:
        raise ValueError("source RGB888 dimensions do not match data length")
    labels = bytearray(width * height)
    start_row = min(height, max(0, round(height * config.roi_top_percent / 100)))
    for y in range(start_row, height):
        for x in range(width):
            index = (y * width + x) * 3
            red, green, blue = rgb[index], rgb[index + 1], rgb[index + 2]
            # Same low-cost rejection as cd_fast_red_yellow_candidate().
            if blue >= red and blue >= green:
                continue
            hue, saturation, lightness = rgb_to_hsl240(red, green, blue)
            label_index = y * width + x
            if config.red.matches(hue, saturation, lightness):
                labels[label_index] = 1
            elif config.yellow.matches(hue, saturation, lightness):
                labels[label_index] = 2

    output = bytearray(width * height * 3)
    red_components = 0
    yellow_components = 0
    for start_index, label in enumerate(labels):
        if label == 0:
            continue
        component = _take_component(labels, width, height, start_index, label)
        if len(component) < config.minimum_area:
            continue
        colour = (255, 63, 63) if label == 1 else (255, 214, 10)
        for pixel in component:
            destination = pixel * 3
            output[destination : destination + 3] = bytes(colour)
        _draw_component_border(output, width, height, component, colour)
        if label == 1:
            red_components += 1
        else:
            yellow_components += 1
    return ColourDetectionOutput(bytes(output), red_components, yellow_components)


def _take_component(
    labels: bytearray, width: int, height: int, start_index: int, label: int
) -> list[int]:
    labels[start_index] = 0
    stack = [start_index]
    component: list[int] = []
    while stack:
        index = stack.pop()
        component.append(index)
        x = index % width
        if x and labels[index - 1] == label:
            labels[index - 1] = 0
            stack.append(index - 1)
        if x + 1 < width and labels[index + 1] == label:
            labels[index + 1] = 0
            stack.append(index + 1)
        if index >= width and labels[index - width] == label:
            labels[index - width] = 0
            stack.append(index - width)
        if index + width < width * height and labels[index + width] == label:
            labels[index + width] = 0
            stack.append(index + width)
    return component


def _draw_component_border(
    output: bytearray, width: int, height: int, component: list[int], colour: tuple[int, int, int]
) -> None:
    xmin = min(index % width for index in component)
    xmax = max(index % width for index in component)
    ymin = min(index // width for index in component)
    ymax = max(index // width for index in component)
    border = (255, 255, 255)
    for x in range(xmin, xmax + 1):
        for y in (ymin, ymax):
            index = (y * width + x) * 3
            output[index : index + 3] = bytes(border)
    for y in range(ymin, ymax + 1):
        for x in (xmin, xmax):
            index = (y * width + x) * 3
            output[index : index + 3] = bytes(border)


class _ImagePreview(ttk.Frame):
    """A canvas that keeps an RGB888 frame fitted to its own size."""

    def __init__(self, parent: tk.Misc, title: str) -> None:
        super().__init__(parent, style="Card.TFrame", padding=(6, 6))
        self.columnconfigure(0, weight=1)
        self.rowconfigure(1, weight=1)
        ttk.Label(self, text=title, style="Section.TLabel", foreground="#93c5fd").grid(
            row=0, column=0, sticky="w", pady=(0, 4)
        )
        self.canvas = tk.Canvas(self, background="#06101f", highlightthickness=0)
        self.canvas.grid(row=1, column=0, sticky="nsew")
        self.canvas.bind("<Configure>", lambda _event: self._render())
        self._rgb = b""
        self._width = 0
        self._height = 0
        self._photo: tk.PhotoImage | None = None
        self._item: int | None = None

    def set_image(self, rgb: bytes, width: int, height: int) -> None:
        self._rgb, self._width, self._height = rgb, width, height
        self._render()

    def _render(self) -> None:
        if not self._rgb or self._width <= 0 or self._height <= 0:
            return
        target_width, target_height = fit_size(
            self._width,
            self._height,
            max(1, self.canvas.winfo_width()),
            max(1, self.canvas.winfo_height()),
        )
        display = resize_rgb888_nearest(
            self._rgb, self._width, self._height, target_width, target_height
        )
        self._photo = tk.PhotoImage(
            master=self.canvas,
            data=rgb888_to_ppm(display, target_width, target_height),
            format="PPM",
        )
        if self._item is None:
            self._item = self.canvas.create_image(0, 0, image=self._photo, anchor="nw")
        else:
            self.canvas.itemconfigure(self._item, image=self._photo)
        self.canvas.coords(
            self._item,
            max(0, (self.canvas.winfo_width() - target_width) // 2),
            max(0, (self.canvas.winfo_height() - target_height) // 2),
        )


class _DualImagePane(ttk.Frame):
    """Shared resizable original/result layout for host-side processors."""

    def __init__(self, parent: tk.Misc, source_label: str, result_label: str) -> None:
        super().__init__(parent, style="Card.TFrame", padding=(8, 8))
        self.columnconfigure(0, weight=1)
        self.rowconfigure(1, weight=1)
        self.controls = ttk.Frame(self, style="Card.TFrame")
        self.controls.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        previews = ttk.Frame(self, style="Card.TFrame")
        previews.grid(row=1, column=0, sticky="nsew")
        previews.columnconfigure(0, weight=1)
        previews.columnconfigure(1, weight=1)
        previews.rowconfigure(0, weight=1)
        self.original_preview = _ImagePreview(previews, source_label)
        self.original_preview.grid(row=0, column=0, sticky="nsew", padx=(0, 4))
        self.result_preview = _ImagePreview(previews, result_label)
        self.result_preview.grid(row=0, column=1, sticky="nsew", padx=(4, 0))


class IpmPane(_DualImagePane):
    """Interactive bird's-eye preview driven by the embedded homography."""

    def __init__(self, parent: tk.Misc, on_change: Callable[[], None]) -> None:
        super().__init__(parent, "实时原图", "逆透视结果（俯视）")
        self._on_change = on_change
        self._processor = IpmProcessor()
        self._frame: tuple[bytes, int, int] | None = None
        self.near_x_var = tk.DoubleVar(value=0.20)
        self.far_x_var = tk.DoubleVar(value=2.00)
        self.half_width_var = tk.DoubleVar(value=0.75)
        self.output_width_var = tk.IntVar(value=240)
        self.output_height_var = tk.IntVar(value=240)

        self.controls.columnconfigure(1, weight=1)
        self.controls.columnconfigure(4, weight=1)
        self._add_scale("近端 X", self.near_x_var, 0.10, 1.50, 0, 0, "{:.2f}")
        self._add_scale("远端 X", self.far_x_var, 0.40, 5.00, 0, 3, "{:.2f}")
        self._add_scale("半宽 Y", self.half_width_var, 0.20, 2.50, 1, 0, "{:.2f}")
        self._add_spin("输出宽", self.output_width_var, 80, 480, 1, 3)
        self._add_spin("输出高", self.output_height_var, 80, 480, 1, 5)
        ttk.Label(
            self.controls,
            text="参数沿用 CameraIPM 标定；输入尺寸会按 160×128 标定基准换算。",
            style="Section.TLabel",
            foreground="#94a3b8",
        ).grid(row=2, column=0, columnspan=7, sticky="w", pady=(5, 0))

    def _add_scale(
        self,
        label: str,
        variable: tk.DoubleVar,
        minimum: float,
        maximum: float,
        row: int,
        column: int,
        pattern: str,
    ) -> None:
        value = tk.StringVar(value=pattern.format(variable.get()))
        ttk.Label(self.controls, text=label, style="Section.TLabel").grid(
            row=row, column=column, sticky="w", padx=(0, 4)
        )
        scale = ttk.Scale(
            self.controls,
            from_=minimum,
            to=maximum,
            variable=variable,
            command=lambda _value: self._settings_changed(value, variable, pattern),
        )
        scale.grid(row=row, column=column + 1, sticky="ew", padx=(0, 4))
        ttk.Label(self.controls, textvariable=value, style="Section.TLabel", foreground="#bfdbfe").grid(
            row=row, column=column + 2, sticky="e", padx=(0, 8)
        )

    def _add_spin(
        self, label: str, variable: tk.IntVar, minimum: int, maximum: int, row: int, column: int
    ) -> None:
        ttk.Label(self.controls, text=label, style="Section.TLabel").grid(
            row=row, column=column, sticky="w", padx=(0, 4)
        )
        spin = ttk.Spinbox(
            self.controls, from_=minimum, to=maximum, increment=8, width=5, textvariable=variable,
            command=self._settings_changed, style="Input.TSpinbox",
        )
        spin.grid(row=row, column=column + 1, sticky="w", padx=(0, 8))
        spin.bind("<FocusOut>", lambda _event: self._settings_changed())
        spin.bind("<Return>", lambda _event: self._settings_changed())

    def _settings_changed(
        self, value_label: tk.StringVar | None = None, variable: tk.DoubleVar | None = None, pattern: str = ""
    ) -> None:
        if value_label is not None and variable is not None:
            value_label.set(pattern.format(variable.get()))
        self._on_change()

    def update_frame(self, rgb: bytes, width: int, height: int) -> None:
        self._frame = (rgb, width, height)
        self.original_preview.set_image(rgb, width, height)
        try:
            config = IpmConfig(
                near_x=float(self.near_x_var.get()),
                far_x=float(self.far_x_var.get()),
                half_width_y=float(self.half_width_var.get()),
                output_width=int(self.output_width_var.get()),
                output_height=int(self.output_height_var.get()),
            )
            result = self._processor.process(rgb, width, height, config)
            self.result_preview.set_image(result, config.output_width, config.output_height)
        except (tk.TclError, ValueError) as error:
            self.result_preview.canvas.delete("all")
            self.result_preview.canvas.create_text(
                10, 10, anchor="nw", text=f"逆透视参数无效：{error}", fill="#fb7185"
            )


class ColourDetectionPane(_DualImagePane):
    """Interactive version of the embedded red/yellow HSL detector."""

    def __init__(self, parent: tk.Misc, on_change: Callable[[], None]) -> None:
        super().__init__(parent, "实时原图", "颜色检测结果")
        self._on_change = on_change
        self._frame: tuple[bytes, int, int] | None = None
        self.info_var = tk.StringVar(value="等待图像")
        self.red_vars = self._make_threshold_variables((230, 15, 20, 240, 65, 240))
        self.yellow_vars = self._make_threshold_variables((18, 42, 80, 240, 70, 240))
        self.roi_top_var = tk.IntVar(value=25)
        self.minimum_area_var = tk.IntVar(value=10)

        red_group = self._add_threshold_group("红色 HSL（0–240）", self.red_vars, 0)
        yellow_group = self._add_threshold_group("黄色 HSL（0–240）", self.yellow_vars, 1)
        generic_group = ttk.LabelFrame(self.controls, text=" 组件过滤 ", padding=(6, 4))
        generic_group.grid(row=0, column=2, sticky="nsew", padx=(8, 0))
        self._add_spin_field(generic_group, "ROI 顶部 (%)", self.roi_top_var, 0, 95, 0)
        self._add_spin_field(generic_group, "最小面积", self.minimum_area_var, 1, 2000, 1)
        ttk.Label(generic_group, textvariable=self.info_var, foreground="#93c5fd").grid(
            row=2, column=0, columnspan=2, sticky="w", pady=(4, 0)
        )
        self.controls.columnconfigure(0, weight=1)
        self.controls.columnconfigure(1, weight=1)
        red_group.columnconfigure(1, weight=1)
        yellow_group.columnconfigure(1, weight=1)

    @staticmethod
    def _make_threshold_variables(values: tuple[int, int, int, int, int, int]) -> tuple[tk.IntVar, ...]:
        return tuple(tk.IntVar(value=value) for value in values)

    def _add_threshold_group(
        self, title: str, variables: tuple[tk.IntVar, ...], column: int
    ) -> ttk.LabelFrame:
        group = ttk.LabelFrame(self.controls, text=f" {title} ", padding=(6, 4))
        group.grid(row=0, column=column, sticky="nsew", padx=(0, 8) if column == 0 else (0, 0))
        for row, (label, variable) in enumerate(zip(("H 最小", "H 最大", "S 最小", "S 最大", "L 最小", "L 最大"), variables)):
            self._add_spin_field(group, label, variable, 0, 240, row)
        return group

    def _add_spin_field(
        self, parent: tk.Misc, label: str, variable: tk.IntVar, minimum: int, maximum: int, row: int
    ) -> None:
        ttk.Label(parent, text=label, style="Section.TLabel").grid(row=row, column=0, sticky="w", padx=(0, 4))
        spin = ttk.Spinbox(
            parent, from_=minimum, to=maximum, increment=1, width=5, textvariable=variable,
            command=self._on_change, style="Input.TSpinbox",
        )
        spin.grid(row=row, column=1, sticky="ew")
        spin.bind("<FocusOut>", lambda _event: self._on_change())
        spin.bind("<Return>", lambda _event: self._on_change())

    @staticmethod
    def _threshold(values: tuple[tk.IntVar, ...]) -> HslThreshold:
        return HslThreshold(*(max(0, min(240, int(value.get()))) for value in values))

    def update_frame(self, rgb: bytes, width: int, height: int) -> None:
        self._frame = (rgb, width, height)
        self.original_preview.set_image(rgb, width, height)
        try:
            config = ColourDetectionConfig(
                red=self._threshold(self.red_vars),
                yellow=self._threshold(self.yellow_vars),
                roi_top_percent=max(0, min(95, int(self.roi_top_var.get()))),
                minimum_area=max(1, int(self.minimum_area_var.get())),
            )
            result = detect_colours(rgb, width, height, config)
            self.result_preview.set_image(result.image, width, height)
            self.info_var.set(f"红色 {result.red_components} 个 · 黄色 {result.yellow_components} 个")
        except (tk.TclError, ValueError) as error:
            self.info_var.set(f"参数无效：{error}")
