"""Live host-side IPM and colour-detection previews.

The calibration and HSL conventions mirror ``EmbedCode/CameraIPM.*`` and
``EmbedCode/ColorDetection.*``.  Processing stays on the PC: it never changes
the received RGB565 frame or the image-saving path.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import asdict, dataclass
import colorsys
import json
import math
from pathlib import Path
import tkinter as tk
from tkinter import messagebox, ttk

from protocol import fit_size, resize_rgb888_nearest, rgb888_to_ppm
import theme as ui


@dataclass(frozen=True)
class CameraCalibration:
    """All values from ``EmbedCode/CameraIPM.h`` in editable form."""

    reference_width: int = 160
    reference_height: int = 128
    fx: float = 86.0473887
    fy: float = 88.6329816
    cx: float = 82.4574101
    cy: float = 66.3210952
    skew: float = 0.0
    radial_count: int = 2
    k: tuple[float, float, float, float, float, float] = (
        0.018489635, -0.028478149, 0.0, 0.0, 0.0, 0.0,
    )
    p1: float = 0.0
    p2: float = 0.0
    homography: tuple[float, float, float, float, float, float, float, float, float] = (
        0.00126378934, 0.176208985, -48.1200764,
        0.447881955, 0.00593926103, -36.8896207,
        0.0173753584, -1.03867922, 1.0,
    )


DEFAULT_CAMERA_CALIBRATION = CameraCalibration()


@dataclass(frozen=True)
class IpmConfig:
    """Bird's-eye output range, in the embedded calibration ground units."""

    near_x: float = 0.20
    far_x: float = 2.00
    half_width_y: float = 0.75
    output_width: int = 240
    output_height: int = 240
    calibration: CameraCalibration = DEFAULT_CAMERA_CALIBRATION


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


def _ground_to_raw_pixel(
    ground_x: float,
    ground_y: float,
    calibration: CameraCalibration,
    inverse_h: tuple[float, ...],
) -> tuple[float, float] | None:
    """Invert the C pipeline: ground -> undistorted pixel -> raw pixel."""

    w = inverse_h[6] * ground_x + inverse_h[7] * ground_y + inverse_h[8]
    if abs(w) < 1.0e-12:
        return None
    uu = (inverse_h[0] * ground_x + inverse_h[1] * ground_y + inverse_h[2]) / w
    vu = (inverse_h[3] * ground_x + inverse_h[4] * ground_y + inverse_h[5]) / w
    y = (vu - calibration.cy) / calibration.fy
    x = (uu - calibration.cx - calibration.skew * y) / calibration.fx
    r2 = x * x + y * y
    r4 = r2 * r2
    r6 = r4 * r2
    radial_count = calibration.radial_count
    numerator = 1.0 + calibration.k[0] * r2 + calibration.k[1] * r4
    denominator = 1.0
    if radial_count >= 3:
        numerator += calibration.k[2] * r6
    if radial_count >= 6:
        denominator += calibration.k[3] * r2 + calibration.k[4] * r4 + calibration.k[5] * r6
    if abs(denominator) < 1.0e-12:
        return None
    radial = numerator / denominator
    xd = x * radial + 2.0 * calibration.p1 * x * y + calibration.p2 * (r2 + 2.0 * x * x)
    yd = y * radial + calibration.p1 * (r2 + 2.0 * y * y) + 2.0 * calibration.p2 * x * y
    raw_u = calibration.fx * xd + calibration.skew * yd + calibration.cx
    raw_v = calibration.fy * yd + calibration.cy
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
        _validate_ipm_config(config)

        lookup: list[int] = []
        calibration = config.calibration
        if calibration.reference_width <= 0 or calibration.reference_height <= 0:
            raise ValueError("calibration reference size must be positive")
        inverse_h = _invert_3x3(calibration.homography)
        x_scale = source_width / calibration.reference_width
        y_scale = source_height / calibration.reference_height
        for output_y in range(config.output_height):
            x_fraction = output_y / max(1, config.output_height - 1)
            ground_x = config.far_x - x_fraction * (config.far_x - config.near_x)
            for output_x in range(config.output_width):
                y_fraction = output_x / max(1, config.output_width - 1)
                ground_y = config.half_width_y - y_fraction * (2.0 * config.half_width_y)
                source_point = _ground_to_raw_pixel(ground_x, ground_y, calibration, inverse_h)
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
    area_min: int = 10
    area_max: int = 12000
    width_min: int = 2
    height_min: int = 3
    fill_min_permille: int = 70
    aspect_min_permille: int = 300
    aspect_max_permille: int = 5000

    def matches(self, h: int, s: int, l: int) -> bool:
        hue_matches = (
            self.h_min <= h <= self.h_max
            if self.h_min <= self.h_max
            else h >= self.h_min or h <= self.h_max
        )
        return hue_matches and self.s_min <= s <= self.s_max and self.l_min <= l <= self.l_max


@dataclass(frozen=True)
class DetectionColour:
    name: str
    threshold: HslThreshold
    preview_rgb: tuple[int, int, int]


DEFAULT_COLOURS = (
    DetectionColour("红色", HslThreshold(230, 15, 20, 240, 65, 240), (255, 63, 63)),
    DetectionColour("黄色", HslThreshold(18, 42, 80, 240, 70, 240), (255, 214, 10)),
)


@dataclass(frozen=True)
class ColourDetectionConfig:
    """Ordered detection rules; the first matching HSL rule owns a pixel."""

    colours: tuple[DetectionColour, ...] = DEFAULT_COLOURS
    roi_top_percent: int = 25
    roi_left_percent: int = 0
    roi_right_percent: int = 100
    maximum_components: int = 12


@dataclass(frozen=True)
class ColourDetectionOutput:
    image: bytes
    component_counts: tuple[int, ...]

    @property
    def red_components(self) -> int:
        return self.component_counts[0] if self.component_counts else 0

    @property
    def yellow_components(self) -> int:
        return self.component_counts[1] if len(self.component_counts) > 1 else 0


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
            hue = 80 + int(40 * (blue - red) / difference)
        else:
            hue = 160 + int(40 * (red - green) / difference)
        denominator = maximum + minimum if lightness <= 120 else 510 - (maximum + minimum)
        saturation = difference * 240 // denominator if denominator else 0
    return max(0, min(240, hue)), max(0, min(240, saturation)), max(0, min(240, lightness))


def detect_colours(
    rgb: bytes, width: int, height: int, config: ColourDetectionConfig
) -> ColourDetectionOutput:
    """Apply ordered HSL rules and four-connected area filters."""

    if len(rgb) != width * height * 3:
        raise ValueError("source RGB888 dimensions do not match data length")
    _validate_colour_config(config)
    labels = [0] * (width * height)
    start_row = min(height, max(0, round(height * config.roi_top_percent / 100)))
    start_column = min(width, max(0, round(width * config.roi_left_percent / 100)))
    end_column = min(width, max(start_column, round(width * config.roi_right_percent / 100)))
    for y in range(start_row, height):
        for x in range(start_column, end_column):
            index = (y * width + x) * 3
            red, green, blue = rgb[index], rgb[index + 1], rgb[index + 2]
            hue, saturation, lightness = rgb_to_hsl240(red, green, blue)
            label_index = y * width + x
            for colour_index, colour in enumerate(config.colours, 1):
                if colour.threshold.matches(hue, saturation, lightness):
                    labels[label_index] = colour_index
                    break

    output = bytearray(width * height * 3)
    counts = [0] * len(config.colours)
    for start_index, label in enumerate(labels):
        if label == 0:
            continue
        component = _take_component(labels, width, height, start_index, label)
        colour_rule = config.colours[label - 1]
        threshold = colour_rule.threshold
        if not _component_passes_geometry(component, width, threshold):
            continue
        if counts[label - 1] >= config.maximum_components:
            continue
        colour = colour_rule.preview_rgb
        for pixel in component:
            destination = pixel * 3
            output[destination : destination + 3] = bytes(colour)
        _draw_component_border(output, width, height, component, colour)
        counts[label - 1] += 1
    return ColourDetectionOutput(bytes(output), tuple(counts))


def _component_passes_geometry(
    component: list[int], width: int, threshold: HslThreshold
) -> bool:
    area = len(component)
    if area < threshold.area_min or area > threshold.area_max:
        return False
    xmin = min(index % width for index in component)
    xmax = max(index % width for index in component)
    ymin = min(index // width for index in component)
    ymax = max(index // width for index in component)
    component_width = xmax - xmin + 1
    component_height = ymax - ymin + 1
    if component_width < threshold.width_min or component_height < threshold.height_min:
        return False
    bbox_area = component_width * component_height
    fill_permille = area * 1000 // bbox_area
    aspect_permille = component_height * 1000 // component_width
    return (
        fill_permille >= threshold.fill_min_permille
        and threshold.aspect_min_permille <= aspect_permille <= threshold.aspect_max_permille
    )


def _take_component(
    labels: list[int], width: int, height: int, start_index: int, label: int
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


class ProcessingConfigError(ValueError):
    """Raised when an imported image-processing JSON file is malformed."""


def save_processing_config(path: str | Path, ipm: IpmConfig, colour: ColourDetectionConfig) -> None:
    """Write both host-side processing configurations in a portable JSON file."""

    _validate_ipm_config(ipm)
    _validate_colour_config(colour)
    payload = {
        "format": "IMGT-processing-config",
        "version": 2,
        "ipm": asdict(ipm),
        "colour_detection": asdict(colour),
    }
    Path(path).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _c_float(value: float) -> str:
    """Format one finite host value as an explicitly single-precision C literal."""

    text = format(value, ".10g")
    if "." not in text and "e" not in text.lower():
        text += ".0"
    return f"({text}f)"


def _c_uint(value: int) -> str:
    return f"({value}u)"


def render_embedded_processing_config(ipm: IpmConfig, colour: ColourDetectionConfig) -> str:
    """Render the complete, replace-in-place C configuration header.

    ``CameraIPM`` and ``ColorDetection`` consume only the macros in this
    header.  Keeping the values here lets the desktop UI export a single file
    that can be copied to the target project without hand-editing C sources.
    """

    _validate_ipm_config(ipm)
    _validate_colour_config(colour)
    calibration = ipm.calibration

    lines = [
        "/*",
        " * TC4ImageProcessingConfig.h",
        " *",
        " * Generated by IMGT Image Monitor.  Replace the whole file with this",
        " * export, then rebuild the embedded project.  Do not edit CameraIPM.c,",
        " * CameraIPM.h, ColorDetection.c, or ColorDetection.h for a new tuning.",
        " */",
        "",
        "#ifndef TC4_IMAGE_PROCESSING_CONFIG_H_",
        "#define TC4_IMAGE_PROCESSING_CONFIG_H_",
        "",
        "#define TC4_IMAGE_PROCESSING_CONFIG_VERSION       (1u)",
        "",
        "/* Camera calibration. */",
        f"#define TC4_IPM_REFERENCE_WIDTH                   {_c_uint(calibration.reference_width)}",
        f"#define TC4_IPM_REFERENCE_HEIGHT                  {_c_uint(calibration.reference_height)}",
        f"#define TC4_IPM_FX                                {_c_float(calibration.fx)}",
        f"#define TC4_IPM_FY                                {_c_float(calibration.fy)}",
        f"#define TC4_IPM_CX                                {_c_float(calibration.cx)}",
        f"#define TC4_IPM_CY                                {_c_float(calibration.cy)}",
        f"#define TC4_IPM_SKEW                              {_c_float(calibration.skew)}",
        f"#define TC4_IPM_RADIAL_COUNT                      {_c_uint(calibration.radial_count)}",
        f"#define TC4_IPM_K1                                {_c_float(calibration.k[0])}",
        f"#define TC4_IPM_K2                                {_c_float(calibration.k[1])}",
        f"#define TC4_IPM_K3                                {_c_float(calibration.k[2])}",
        f"#define TC4_IPM_K4                                {_c_float(calibration.k[3])}",
        f"#define TC4_IPM_K5                                {_c_float(calibration.k[4])}",
        f"#define TC4_IPM_K6                                {_c_float(calibration.k[5])}",
        f"#define TC4_IPM_P1                                {_c_float(calibration.p1)}",
        f"#define TC4_IPM_P2                                {_c_float(calibration.p2)}",
        "",
        "/* Homography: undistorted pixel [u v 1]^T -> ground [X Y 1]^T. */",
    ]
    for row in range(3):
        for column in range(3):
            value = calibration.homography[row * 3 + column]
            lines.append(f"#define TC4_IPM_H{row + 1}{column + 1}                               {_c_float(value)}")
    lines.extend((
        "",
        "/* Bird's-eye view geometry. */",
        f"#define TC4_IPM_VIEW_NEAR_X                       {_c_float(ipm.near_x)}",
        f"#define TC4_IPM_VIEW_FAR_X                        {_c_float(ipm.far_x)}",
        f"#define TC4_IPM_VIEW_HALF_WIDTH_Y                 {_c_float(ipm.half_width_y)}",
        f"#define TC4_IPM_VIEW_OUTPUT_WIDTH                 {_c_uint(ipm.output_width)}",
        f"#define TC4_IPM_VIEW_OUTPUT_HEIGHT                {_c_uint(ipm.output_height)}",
        "",
        "/* Host-configurable ordered detector settings. */",
        f"#define TC4_CFG_COLOR_COUNT                       {_c_uint(len(colour.colours))}",
        f"#define TC4_CFG_COLOR_MAX_CONES                   {_c_uint(colour.maximum_components)}",
        f"#define TC4_CFG_COLOR_ROI_TOP_PERCENT             {_c_uint(colour.roi_top_percent)}",
        f"#define TC4_CFG_COLOR_ROI_LEFT_PERCENT            {_c_uint(colour.roi_left_percent)}",
        f"#define TC4_CFG_COLOR_ROI_RIGHT_PERCENT           {_c_uint(colour.roi_right_percent)}",
        "",
    ))
    lines.append("/* H, S, L, area, width, height, fill and aspect for each rule. */")
    lines.append("#define TC4_CFG_COLOR_THRESHOLDS \\")
    for index, item in enumerate(colour.colours):
        t = item.threshold
        values = (
            t.h_min, t.h_max, t.s_min, t.s_max, t.l_min, t.l_max,
            t.area_min, t.area_max, t.width_min, t.height_min,
            t.fill_min_permille, t.aspect_min_permille, t.aspect_max_permille,
        )
        suffix = " \\" if index + 1 < len(colour.colours) else ""
        lines.append("    {" + ", ".join(_c_uint(value) for value in values) + "}," + suffix)
    lines.append("")
    lines.extend((
        "/* Embedded-only safeguards, fixed in every host export. */",
        "#define TC4_CFG_COLOR_SWAP_RGB565_BYTES           (1u)",
        "#define TC4_CFG_COLOR_BOTTOM_BAND_ROWS             (3u)",
        "#define TC4_CFG_COLOR_BOTTOM_MIN_PIXELS            (2u)",
        "#define TC4_CFG_COLOR_COPY_CAMERA_FRAME            (0u)",
        "#define TC4_CFG_WHITE_MIN_CHANNEL                  (200u)",
        "#define TC4_CFG_WHITE_MAX_CHROMA                   (40u)",
        "#define TC4_CFG_WHITE_MIN_AREA                     (200u)",
        "#define TC4_CFG_WHITE_ROI_TOP_PERCENT              (25u)",
        "",
        "#endif /* TC4_IMAGE_PROCESSING_CONFIG_H_ */",
        "",
    ))
    return "\n".join(lines)


def save_embedded_processing_config(
    path: str | Path, ipm: IpmConfig, colour: ColourDetectionConfig
) -> None:
    """Write the C header produced by :func:`render_embedded_processing_config`."""

    Path(path).write_text(render_embedded_processing_config(ipm, colour), encoding="utf-8")


def load_processing_config(path: str | Path) -> tuple[IpmConfig, ColourDetectionConfig]:
    """Load and validate a configuration exported by :func:`save_processing_config`."""

    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        if payload.get("format") != "IMGT-processing-config" or payload.get("version") not in (1, 2):
            raise ProcessingConfigError("不是受支持的 IMGT 图像处理配置文件")
        ipm_data = payload["ipm"]
        colour_data = payload["colour_detection"]
        calibration_data = ipm_data["calibration"]
        calibration = CameraCalibration(
            reference_width=int(calibration_data["reference_width"]),
            reference_height=int(calibration_data["reference_height"]),
            fx=float(calibration_data["fx"]), fy=float(calibration_data["fy"]),
            cx=float(calibration_data["cx"]), cy=float(calibration_data["cy"]),
            skew=float(calibration_data["skew"]), radial_count=int(calibration_data["radial_count"]),
            k=_float_tuple(calibration_data["k"], 6, "k"),
            p1=float(calibration_data["p1"]), p2=float(calibration_data["p2"]),
            homography=_float_tuple(calibration_data["homography"], 9, "homography"),
        )
        ipm = IpmConfig(
            near_x=float(ipm_data["near_x"]), far_x=float(ipm_data["far_x"]),
            half_width_y=float(ipm_data["half_width_y"]),
            output_width=int(ipm_data["output_width"]), output_height=int(ipm_data["output_height"]),
            calibration=calibration,
        )
        if payload["version"] == 1:
            colours = (
                DetectionColour("红色", _threshold_from_dict(colour_data["red"]), (255, 63, 63)),
                DetectionColour("黄色", _threshold_from_dict(colour_data["yellow"]), (255, 214, 10)),
            )
        else:
            colours = tuple(
                DetectionColour(
                    str(item["name"]), _threshold_from_dict(item["threshold"]),
                    tuple(int(channel) for channel in item["preview_rgb"]),
                )
                for item in colour_data["colours"]
            )
        colour = ColourDetectionConfig(
            colours=colours,
            roi_top_percent=int(colour_data["roi_top_percent"]),
            roi_left_percent=int(colour_data["roi_left_percent"]),
            roi_right_percent=int(colour_data["roi_right_percent"]),
            maximum_components=int(colour_data["maximum_components"]),
        )
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
        raise ProcessingConfigError(f"配置内容无效：{error}") from error
    _validate_ipm_config(ipm)
    _validate_colour_config(colour)
    return ipm, colour


def _float_tuple(value: object, length: int, name: str) -> tuple[float, ...]:
    if not isinstance(value, (list, tuple)) or len(value) != length:
        raise ProcessingConfigError(f"{name} 必须包含 {length} 个数值")
    return tuple(float(item) for item in value)


def _threshold_from_dict(value: object) -> HslThreshold:
    if not isinstance(value, dict):
        raise ProcessingConfigError("颜色阈值必须是对象")
    fields = (
        "h_min", "h_max", "s_min", "s_max", "l_min", "l_max",
        "area_min", "area_max", "width_min", "height_min",
        "fill_min_permille", "aspect_min_permille", "aspect_max_permille",
    )
    return HslThreshold(*(int(value[field]) for field in fields))


def _validate_ipm_config(config: IpmConfig) -> None:
    calibration = config.calibration
    calibration_numbers = (
        calibration.fx, calibration.fy, calibration.cx, calibration.cy, calibration.skew,
        *calibration.k, calibration.p1, calibration.p2, *calibration.homography,
    )
    if (
        config.output_width < 8 or config.output_height < 8
        or config.output_width > 65535 or config.output_height > 65535
        or config.far_x <= config.near_x or config.half_width_y <= 0.0
        or calibration.reference_width <= 0 or calibration.reference_height <= 0
        or calibration.reference_width > 65535 or calibration.reference_height > 65535
        or abs(calibration.fx) < 1.0e-12 or abs(calibration.fy) < 1.0e-12
        or calibration.radial_count not in (2, 3, 6)
        or not all(math.isfinite(value) for value in (config.near_x, config.far_x, config.half_width_y, *calibration_numbers))
    ):
        raise ProcessingConfigError("逆透视参数范围无效")
    _invert_3x3(calibration.homography)


def _validate_colour_config(config: ColourDetectionConfig) -> None:
    if not (
        0 <= config.roi_top_percent <= 100
        and 0 <= config.roi_left_percent < config.roi_right_percent <= 100
        and 0 < config.maximum_components <= 255
    ):
        raise ProcessingConfigError("颜色检测 ROI 或组件数量范围无效")
    if not config.colours or len(config.colours) > 65534:
        raise ProcessingConfigError("至少保留一种颜色；嵌入式标签编号最多支持 65534 种")
    for colour in config.colours:
        if not colour.name.strip() or len(colour.name) > 64:
            raise ProcessingConfigError("颜色名称不能为空且不得超过 64 字符")
        if len(colour.preview_rgb) != 3 or any(not 0 <= value <= 255 for value in colour.preview_rgb):
            raise ProcessingConfigError("预览颜色 RGB 必须为 0–255")
        threshold = colour.threshold
        if (
            not all(0 <= value <= 240 for value in (
                threshold.h_min, threshold.h_max, threshold.s_min, threshold.s_max,
                threshold.l_min, threshold.l_max,
            ))
            or threshold.area_min <= 0 or threshold.area_max < threshold.area_min
            or threshold.area_max > 65535 or threshold.width_min > 65535
            or threshold.height_min > 65535 or threshold.fill_min_permille < 0
            or threshold.fill_min_permille > 65535
            or threshold.aspect_min_permille > 65535 or threshold.aspect_max_permille > 65535
            or threshold.aspect_min_permille < 0
            or threshold.aspect_max_permille < threshold.aspect_min_permille
        ):
            raise ProcessingConfigError("颜色阈值范围无效")


class _ImagePreview(ttk.Frame):
    """A canvas that keeps an RGB888 frame fitted to its own size."""

    def __init__(self, parent: tk.Misc, title: str) -> None:
        super().__init__(parent, style="Card.TFrame", padding=(6, 6))
        self.columnconfigure(0, weight=1)
        self.rowconfigure(1, weight=1)
        ttk.Label(self, text=title, style="Section.TLabel", foreground=ui.MUTED).grid(
            row=0, column=0, sticky="w", pady=(0, 4)
        )
        self.canvas = tk.Canvas(self, background=ui.PLOT, highlightthickness=0)
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
        # At laptop height the colour controls are taller than the preview
        # page.  A bounded scroll area preserves every editor and button while
        # keeping both image canvases visible.
        self._control_canvas = tk.Canvas(
            self, background=ui.SURFACE, highlightthickness=0, borderwidth=0, height=118
        )
        self._control_canvas.grid(row=0, column=0, sticky="ew", pady=(0, 5))
        self._control_scrollbar = ttk.Scrollbar(
            self, orient="vertical", command=self._control_canvas.yview
        )
        self._control_scrollbar.grid(row=0, column=1, sticky="ns", pady=(0, 5))
        self._control_canvas.configure(yscrollcommand=self._control_scrollbar.set)
        self.controls = ttk.Frame(self._control_canvas, style="Card.TFrame")
        self._control_window = self._control_canvas.create_window(
            0, 0, window=self.controls, anchor="nw"
        )
        self.controls.bind("<Configure>", self._on_controls_resize)
        self._control_canvas.bind("<Configure>", self._on_control_viewport_resize)
        self._control_canvas.bind("<MouseWheel>", self._on_control_wheel)
        self.bind("<Configure>", self._limit_control_height)
        self.after_idle(self._bind_control_wheel)
        previews = ttk.Frame(self, style="Card.TFrame")
        previews.grid(row=1, column=0, columnspan=2, sticky="nsew")
        previews.columnconfigure(0, weight=1)
        previews.columnconfigure(1, weight=1)
        previews.rowconfigure(0, weight=1)
        self.original_preview = _ImagePreview(previews, source_label)
        self.original_preview.grid(row=0, column=0, sticky="nsew", padx=(0, 4))
        self.result_preview = _ImagePreview(previews, result_label)
        self.result_preview.grid(row=0, column=1, sticky="nsew", padx=(4, 0))

    def _on_controls_resize(self, _event: tk.Event) -> None:
        self._control_canvas.configure(scrollregion=self._control_canvas.bbox("all"))

    def _on_control_viewport_resize(self, event: tk.Event) -> None:
        self._control_canvas.itemconfigure(self._control_window, width=event.width)

    def _limit_control_height(self, event: tk.Event) -> None:
        content_height = self.controls.winfo_reqheight()
        available = max(68, int(event.height * 0.34))
        target = min(content_height, available)
        if abs(self._control_canvas.winfo_height() - target) > 1:
            self._control_canvas.configure(height=target)

    def _bind_control_wheel(self, widget: tk.Misc | None = None) -> None:
        def bind_descendants(child_widget: tk.Misc) -> None:
            if not getattr(child_widget, "_control_wheel_bound", False):
                child_widget.bind("<MouseWheel>", self._on_control_wheel, add="+")
                child_widget._control_wheel_bound = True
            for child in child_widget.winfo_children():
                bind_descendants(child)

        bind_descendants(widget or self.controls)

    def _on_control_wheel(self, event: tk.Event) -> str:
        if event.delta:
            self._control_canvas.yview_scroll(-1 if event.delta > 0 else 1, "units")
        return "break"


class IpmPane(_DualImagePane):
    """Interactive bird's-eye preview driven by the embedded homography."""

    def __init__(self, parent: tk.Misc, on_change: Callable[[], None]) -> None:
        super().__init__(parent, "实时原图", "逆透视结果（俯视）")
        self._on_change = on_change
        self._processor = IpmProcessor()
        self._frame: tuple[bytes, int, int] | None = None
        self.calibration = DEFAULT_CAMERA_CALIBRATION
        self._scale_value_labels: list[tuple[tk.StringVar, tk.DoubleVar, str]] = []
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
        ttk.Button(self.controls, text="标定参数…", command=self._open_calibration_editor).grid(
            row=2, column=0, sticky="w", pady=(5, 0)
        )
        ttk.Label(
            self.controls,
            text="参数默认与 CameraIPM.h 一致；标定窗口可编辑全部相机与单应矩阵参数。",
            style="Section.TLabel",
            foreground=ui.MUTED,
        ).grid(row=2, column=1, columnspan=6, sticky="w", pady=(5, 0))

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
        self._scale_value_labels.append((value, variable, pattern))
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
        ttk.Label(self.controls, textvariable=value, style="Section.TLabel", foreground=ui.TEXT).grid(
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

    def get_config(self) -> IpmConfig:
        return IpmConfig(
            near_x=float(self.near_x_var.get()),
            far_x=float(self.far_x_var.get()),
            half_width_y=float(self.half_width_var.get()),
            output_width=int(self.output_width_var.get()),
            output_height=int(self.output_height_var.get()),
            calibration=self.calibration,
        )

    def apply_config(self, config: IpmConfig) -> None:
        _validate_ipm_config(config)
        self.near_x_var.set(config.near_x)
        self.far_x_var.set(config.far_x)
        self.half_width_var.set(config.half_width_y)
        self.output_width_var.set(config.output_width)
        self.output_height_var.set(config.output_height)
        self.calibration = config.calibration
        for value_label, variable, pattern in self._scale_value_labels:
            value_label.set(pattern.format(variable.get()))
        self._on_change()

    def _open_calibration_editor(self) -> None:
        window = tk.Toplevel(self)
        window.title("CameraIPM 标定参数")
        window.configure(background=ui.CANVAS)
        window.minsize(700, 420)
        window.geometry("780x470")
        window.transient(self.winfo_toplevel())
        window.columnconfigure(0, weight=1)
        window.rowconfigure(0, weight=1)
        content = ttk.Frame(window, style="Card.TFrame", padding=(12, 10))
        content.grid(row=0, column=0, sticky="nsew")
        content.columnconfigure(0, weight=1)
        content.columnconfigure(1, weight=1)
        content.columnconfigure(2, weight=1)

        calibration = self.calibration
        values: dict[str, tk.StringVar] = {}

        def add_group(
            column: int, title: str, fields: list[tuple[str, str, float | int]]
        ) -> None:
            group = ttk.LabelFrame(content, text=f" {title} ", padding=(8, 6))
            group.grid(row=0, column=column, sticky="nsew", padx=(0, 8) if column < 2 else (0, 0))
            group.columnconfigure(1, weight=1)
            for row, (key, label, value) in enumerate(fields):
                values[key] = tk.StringVar(value=str(value))
                ttk.Label(group, text=label, style="Section.TLabel").grid(
                    row=row, column=0, sticky="w", padx=(0, 5), pady=2
                )
                ttk.Entry(group, textvariable=values[key], style="Input.TEntry", width=13).grid(
                    row=row, column=1, sticky="ew", pady=2
                )

        add_group(0, "内参与图像尺寸", [
            ("reference_width", "标定宽", calibration.reference_width),
            ("reference_height", "标定高", calibration.reference_height),
            ("fx", "FX", calibration.fx), ("fy", "FY", calibration.fy),
            ("cx", "CX", calibration.cx), ("cy", "CY", calibration.cy),
            ("skew", "Skew", calibration.skew), ("radial_count", "径向项数", calibration.radial_count),
        ])
        add_group(1, "畸变参数", [
            *((f"k{index}", f"K{index + 1}", calibration.k[index]) for index in range(6)),
            ("p1", "P1", calibration.p1), ("p2", "P2", calibration.p2),
        ])
        add_group(2, "单应矩阵 H", [
            *((f"h{index}", f"H{index // 3 + 1}{index % 3 + 1}", calibration.homography[index]) for index in range(9)),
        ])

        actions = ttk.Frame(content, style="Card.TFrame")
        actions.grid(row=1, column=0, columnspan=3, sticky="e", pady=(10, 0))

        def apply() -> None:
            try:
                updated = CameraCalibration(
                    reference_width=int(values["reference_width"].get()),
                    reference_height=int(values["reference_height"].get()),
                    fx=float(values["fx"].get()), fy=float(values["fy"].get()),
                    cx=float(values["cx"].get()), cy=float(values["cy"].get()),
                    skew=float(values["skew"].get()), radial_count=int(values["radial_count"].get()),
                    k=tuple(float(values[f"k{index}"].get()) for index in range(6)),
                    p1=float(values["p1"].get()), p2=float(values["p2"].get()),
                    homography=tuple(float(values[f"h{index}"].get()) for index in range(9)),
                )
                self.apply_config(IpmConfig(
                    near_x=float(self.near_x_var.get()), far_x=float(self.far_x_var.get()),
                    half_width_y=float(self.half_width_var.get()),
                    output_width=int(self.output_width_var.get()),
                    output_height=int(self.output_height_var.get()), calibration=updated,
                ))
            except (ValueError, ProcessingConfigError) as error:
                messagebox.showerror("标定参数无效", str(error), parent=window)
                return
            window.destroy()

        def apply_defaults() -> None:
            self.apply_config(IpmConfig())
            window.destroy()

        ttk.Button(actions, text="应用嵌入式默认值", command=apply_defaults).grid(
            row=0, column=0, padx=3
        )
        ttk.Button(actions, text="取消", command=window.destroy).grid(row=0, column=1, padx=3)
        ttk.Button(actions, text="应用", style="Accent.TButton", command=apply).grid(row=0, column=2, padx=3)

    def update_frame(self, rgb: bytes, width: int, height: int) -> None:
        self._frame = (rgb, width, height)
        self.original_preview.set_image(rgb, width, height)
        try:
            config = self.get_config()
            _validate_ipm_config(config)
            result = self._processor.process(rgb, width, height, config)
            self.result_preview.set_image(result, config.output_width, config.output_height)
        except (tk.TclError, ValueError) as error:
            self.result_preview.canvas.delete("all")
            self.result_preview.canvas.create_text(
                10, 10, anchor="nw", text=f"逆透视参数无效：{error}", fill=ui.ERROR
            )


class ColourDetectionPane(_DualImagePane):
    """Interactive HSL and area editors with always-visible colour actions."""

    def __init__(self, parent: tk.Misc, on_change: Callable[[], None]) -> None:
        super().__init__(parent, "实时原图", "颜色检测结果")
        self._on_change = on_change
        self._frame: tuple[bytes, int, int] | None = None
        self.info_var = tk.StringVar(value="等待图像")
        self._colour_rows: list[dict] = []
        self.colour_count_var = tk.StringVar()
        # Colour actions stay visible above the scrolling threshold editors.
        self._control_canvas.grid_configure(row=1)
        self._control_scrollbar.grid_configure(row=1)
        self.original_preview.master.grid_configure(row=2)
        self.rowconfigure(1, weight=0)
        self.rowconfigure(2, weight=1)
        actions = ttk.Frame(self, style="Card.TFrame")
        actions.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 6))
        ttk.Button(actions, text="增加颜色", style="Accent.TButton", command=self._add_colour).grid(
            row=0, column=0, padx=(0, 6)
        )
        self.reduce_colour_button = ttk.Button(actions, text="减少颜色", command=self._reduce_colour)
        self.reduce_colour_button.grid(row=0, column=1, padx=(0, 8))
        ttk.Label(actions, textvariable=self.colour_count_var, style="Section.TLabel").grid(
            row=0, column=2, sticky="w"
        )
        ttk.Label(actions, text="减少颜色移除末项；每项的移除按钮可删除指定颜色。",
                  style="Section.TLabel").grid(row=1, column=0, columnspan=3, sticky="w", pady=(4, 0))
        self.roi_top_var = tk.IntVar(value=25)
        self.roi_left_var = tk.IntVar(value=0)
        self.roi_right_var = tk.IntVar(value=100)
        self.maximum_components_var = tk.IntVar(value=12)

        generic_group = ttk.LabelFrame(self.controls, text=" 组件过滤 ", padding=(6, 4))
        generic_group.grid(row=1, column=0, sticky="ew", pady=(8, 0))
        self._add_spin_field(generic_group, "ROI 顶部 (%)", self.roi_top_var, 0, 95, 0)
        self._add_spin_field(generic_group, "ROI 左侧 (%)", self.roi_left_var, 0, 99, 1)
        self._add_spin_field(generic_group, "ROI 右侧 (%)", self.roi_right_var, 1, 100, 2)
        self._add_spin_field(generic_group, "每色最大组件", self.maximum_components_var, 1, 99, 3)
        ttk.Label(generic_group, textvariable=self.info_var, foreground=ui.MUTED).grid(
            row=4, column=0, columnspan=2, sticky="w", pady=(4, 0)
        )
        ttk.Button(generic_group, text="完整阈值…", command=self._open_geometry_editor).grid(
            row=5, column=0, columnspan=2, sticky="ew", pady=(5, 0)
        )
        self.colour_container = ttk.Frame(self.controls, style="Card.TFrame")
        self.colour_container.grid(row=0, column=0, sticky="ew")
        self.controls.columnconfigure(0, weight=1)
        self.colour_container.columnconfigure(0, weight=1)
        for colour in DEFAULT_COLOURS:
            self._add_colour(colour, notify=False)

    @staticmethod
    def _make_threshold_variables(values: tuple[int, int, int, int, int, int]) -> tuple[tk.IntVar, ...]:
        return tuple(tk.IntVar(value=value) for value in values)

    @staticmethod
    def _make_geometry_variables(threshold: HslThreshold) -> dict[str, tk.IntVar]:
        return {
            "area_min": tk.IntVar(value=threshold.area_min),
            "area_max": tk.IntVar(value=threshold.area_max),
            "width_min": tk.IntVar(value=threshold.width_min),
            "height_min": tk.IntVar(value=threshold.height_min),
            "fill_min_permille": tk.IntVar(value=threshold.fill_min_permille),
            "aspect_min_permille": tk.IntVar(value=threshold.aspect_min_permille),
            "aspect_max_permille": tk.IntVar(value=threshold.aspect_max_permille),
        }

    def _add_colour(self, colour: DetectionColour | None = None, notify: bool = True) -> None:
        if colour is None:
            number = len(self._colour_rows) + 1
            hue = (number * 47) % 240
            colour = DetectionColour(
                f"颜色 {number}", HslThreshold(max(0, hue - 12), min(240, hue + 12), 80, 240, 65, 240),
                tuple(round(value * 255) for value in colorsys.hls_to_rgb(hue / 240, 0.5, 1.0)),
            )
        group = ttk.LabelFrame(self.colour_container, text=f" {colour.name} · HSL（0–240） ", padding=(8, 6))
        row: dict = {
            "group": group,
            "name": tk.StringVar(value=colour.name),
            "rgb": tk.StringVar(value="#{:02X}{:02X}{:02X}".format(*colour.preview_rgb)),
            "hsl": self._make_threshold_variables(tuple(getattr(colour.threshold, key) for key in
                ("h_min", "h_max", "s_min", "s_max", "l_min", "l_max"))),
            "geometry": self._make_geometry_variables(colour.threshold),
        }
        self._colour_rows.append(row)
        group.grid(row=len(self._colour_rows) - 1, column=0, sticky="ew", pady=(0, 6))
        for column in range(4):
            group.columnconfigure(column, weight=1)
        ttk.Label(group, text="名称", style="Section.TLabel").grid(row=0, column=0, sticky="w")
        name_entry = ttk.Entry(group, textvariable=row["name"], style="Input.TEntry", width=12)
        name_entry.grid(row=0, column=1, sticky="ew", padx=(3, 9))
        name_entry.bind("<FocusOut>", lambda _event: self._on_change())
        name_entry.bind("<Return>", lambda _event: self._on_change())
        ttk.Label(group, text="标记色 #RRGGBB", style="Section.TLabel").grid(row=0, column=2, sticky="w")
        rgb_entry = ttk.Entry(group, textvariable=row["rgb"], style="Input.TEntry", width=9)
        rgb_entry.grid(row=0, column=3, sticky="ew")
        rgb_entry.bind("<FocusOut>", lambda _event: self._on_change())
        rgb_entry.bind("<Return>", lambda _event: self._on_change())
        for index, (label, variable) in enumerate(zip(
            ("H 最小", "H 最大", "S 最小", "S 最大", "L 最小", "L 最大"), row["hsl"]
        )):
            self._add_compact_field(group, label, variable, 0, 240, 1 + index // 2, (index % 2) * 2)
        self._add_compact_field(group, "最小面积(px)", row["geometry"]["area_min"], 1, 65535, 4, 0)
        self._add_compact_field(group, "最大面积(px)", row["geometry"]["area_max"], 1, 65535, 4, 2)
        ttk.Button(group, text="高级阈值…", command=lambda item=row: self._open_geometry_editor(item)).grid(
            row=5, column=0, columnspan=2, sticky="ew", padx=(0, 4), pady=(4, 0)
        )
        ttk.Button(group, text="移除", command=lambda item=row: self._remove_colour(item)).grid(
            row=5, column=2, columnspan=2, sticky="ew", pady=(4, 0)
        )
        self._bind_control_wheel(group)
        self._update_colour_actions()
        if notify:
            self._on_change()

    def _update_colour_actions(self) -> None:
        self.colour_count_var.set(f"当前 {len(self._colour_rows)} 种颜色")
        self.reduce_colour_button.configure(state="normal" if len(self._colour_rows) > 1 else "disabled")

    def _reduce_colour(self) -> None:
        if len(self._colour_rows) > 1:
            self._remove_colour(self._colour_rows[-1])

    def _remove_colour(self, row: dict) -> None:
        if len(self._colour_rows) == 1:
            messagebox.showwarning("颜色检测", "至少保留一种颜色。", parent=self)
            return
        row["group"].destroy()
        self._colour_rows.remove(row)
        for index, item in enumerate(self._colour_rows):
            item["group"].grid_configure(row=index)
        self._update_colour_actions()
        self._on_change()

    def _add_compact_field(
        self, parent: tk.Misc, label: str, variable: tk.IntVar,
        minimum: int, maximum: int, row: int, column: int,
    ) -> None:
        ttk.Label(parent, text=label, style="Section.TLabel").grid(row=row, column=column, sticky="w", pady=2)
        spin = ttk.Spinbox(parent, from_=minimum, to=maximum, width=7, textvariable=variable,
                           command=self._on_change, style="Input.TSpinbox")
        spin.grid(row=row, column=column + 1, sticky="ew", padx=(3, 9), pady=2)
        spin.bind("<FocusOut>", lambda _event: self._on_change())
        spin.bind("<Return>", lambda _event: self._on_change())

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
    def _threshold(values: tuple[tk.IntVar, ...], geometry: dict[str, tk.IntVar]) -> HslThreshold:
        hsl = [max(0, min(240, int(value.get()))) for value in values]
        return HslThreshold(
            *hsl,
            area_min=max(1, int(geometry["area_min"].get())),
            area_max=max(1, int(geometry["area_max"].get())),
            width_min=max(1, int(geometry["width_min"].get())),
            height_min=max(1, int(geometry["height_min"].get())),
            fill_min_permille=max(0, int(geometry["fill_min_permille"].get())),
            aspect_min_permille=max(0, int(geometry["aspect_min_permille"].get())),
            aspect_max_permille=max(0, int(geometry["aspect_max_permille"].get())),
        )

    def get_config(self) -> ColourDetectionConfig:
        colours = []
        for row in self._colour_rows:
            rgb_text = row["rgb"].get().strip().lstrip("#")
            if len(rgb_text) != 6:
                raise ProcessingConfigError("标记色必须是 #RRGGBB 格式")
            try:
                rgb = tuple(int(rgb_text[index:index + 2], 16) for index in (0, 2, 4))
            except ValueError as error:
                raise ProcessingConfigError("标记色必须是 #RRGGBB 格式") from error
            colours.append(DetectionColour(row["name"].get().strip(),
                                           self._threshold(row["hsl"], row["geometry"]), rgb))
        return ColourDetectionConfig(
            colours=tuple(colours),
            roi_top_percent=max(0, min(100, int(self.roi_top_var.get()))),
            roi_left_percent=max(0, min(100, int(self.roi_left_var.get()))),
            roi_right_percent=max(0, min(100, int(self.roi_right_var.get()))),
            maximum_components=max(1, int(self.maximum_components_var.get())),
        )

    def apply_config(self, config: ColourDetectionConfig) -> None:
        _validate_colour_config(config)
        for row in self._colour_rows:
            row["group"].destroy()
        self._colour_rows.clear()
        for colour in config.colours:
            self._add_colour(colour, notify=False)
        self.roi_top_var.set(config.roi_top_percent)
        self.roi_left_var.set(config.roi_left_percent)
        self.roi_right_var.set(config.roi_right_percent)
        self.maximum_components_var.set(config.maximum_components)
        self._on_change()

    @staticmethod
    def _set_threshold_variables(
        hsl_variables: tuple[tk.IntVar, ...], geometry_variables: dict[str, tk.IntVar], threshold: HslThreshold
    ) -> None:
        for variable, value in zip(
            hsl_variables,
            (threshold.h_min, threshold.h_max, threshold.s_min, threshold.s_max, threshold.l_min, threshold.l_max),
        ):
            variable.set(value)
        for key in geometry_variables:
            geometry_variables[key].set(getattr(threshold, key))

    def _open_geometry_editor(self, selected: dict | None = None) -> None:
        if selected is None:
            selected = self._colour_rows[0]
        window = tk.Toplevel(self)
        window.title("ColorDetection 完整组件阈值")
        window.configure(background=ui.CANVAS)
        window.minsize(330, 320)
        window.geometry("400x390")
        window.transient(self.winfo_toplevel())
        window.columnconfigure(0, weight=1)
        window.rowconfigure(0, weight=1)
        content = ttk.Frame(window, style="Card.TFrame", padding=(12, 10))
        content.grid(row=0, column=0, sticky="nsew")
        content.columnconfigure(0, weight=1)
        content.columnconfigure(0, weight=1)
        labels = (
            ("area_min", "最小面积"), ("area_max", "最大面积"),
            ("width_min", "最小宽度"), ("height_min", "最小高度"),
            ("fill_min_permille", "最小填充‰"),
            ("aspect_min_permille", "最小纵横比‰"),
            ("aspect_max_permille", "最大纵横比‰"),
        )
        edit_vars: dict[str, tk.StringVar] = {}
        group = ttk.LabelFrame(content, text=f" {selected['name'].get()}组件过滤 ", padding=(8, 6))
        group.grid(row=0, column=0, sticky="nsew")
        group.columnconfigure(1, weight=1)
        for index, (key, label) in enumerate(labels):
            variable = tk.StringVar(value=str(selected["geometry"][key].get()))
            edit_vars[key] = variable
            ttk.Label(group, text=label, style="Section.TLabel").grid(row=index, column=0, sticky="w", padx=(0, 5), pady=2)
            ttk.Entry(group, textvariable=variable, style="Input.TEntry", width=10).grid(row=index, column=1, sticky="ew", pady=2)

        actions = ttk.Frame(content, style="Card.TFrame")
        actions.grid(row=1, column=0, sticky="e", pady=(10, 0))

        def apply() -> None:
            previous = {key: variable.get() for key, variable in selected["geometry"].items()}
            try:
                parsed = {key: int(variable.get()) for key, variable in edit_vars.items()}
                for key, value in parsed.items():
                    selected["geometry"][key].set(value)
                _validate_colour_config(self.get_config())
            except (tk.TclError, ValueError, ProcessingConfigError) as error:
                for key, value in previous.items():
                    selected["geometry"][key].set(value)
                messagebox.showerror("颜色阈值无效", str(error), parent=window)
                return
            self._on_change()
            window.destroy()

        def apply_defaults() -> None:
            self.apply_config(ColourDetectionConfig())
            window.destroy()

        ttk.Button(actions, text="应用嵌入式默认值", command=apply_defaults).grid(
            row=0, column=0, padx=3
        )
        ttk.Button(actions, text="取消", command=window.destroy).grid(row=0, column=1, padx=3)
        ttk.Button(actions, text="应用", style="Accent.TButton", command=apply).grid(row=0, column=2, padx=3)

    def update_frame(self, rgb: bytes, width: int, height: int) -> None:
        self._frame = (rgb, width, height)
        self.original_preview.set_image(rgb, width, height)
        try:
            config = self.get_config()
            _validate_colour_config(config)
            result = detect_colours(rgb, width, height, config)
            self.result_preview.set_image(result.image, width, height)
            self.info_var.set(" · ".join(
                f"{colour.name} {count} 个" for colour, count in zip(config.colours, result.component_counts)
            ))
        except (tk.TclError, ValueError) as error:
            self.info_var.set(f"参数无效：{error}")
