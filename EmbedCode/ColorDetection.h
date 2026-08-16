/*
 * ColorDetection.h
 *
 * Red / yellow cone detection for SCC8660 RGB565 images.
 *
 * Pipeline:
 *   RGB565 -> fast reject -> HSL threshold (single pass for red+yellow)
 *   -> label map -> 4-connected components
 *   -> geometry filter -> bottom contact pixel
 *   -> Camera_RawPixelToGround()
 *
 * Coordinate convention of the ground point is defined by CameraIPM calibration.
 */

#ifndef CODE_COLORDETECTION_H_
#define CODE_COLORDETECTION_H_

#include "Header.h"
#include "TC4ImageProcessingConfig.h"

#ifdef __cplusplus
extern "C" {
#endif

#define COLOR_DETECTION_MAX_CONES            (TC4_CFG_COLOR_MAX_CONES)

/* Host and target both express ROI as percentages of the current frame.  The
 * +50 form is round-to-nearest for the SCC8660 dimensions and matches the
 * host preview's pixel conversion. */
#define COLOR_DETECTION_PERCENT_TO_X(percent) \
    ((uint16)(((uint32)SCC8660_W * (uint32)(percent) + 50u) / 100u))
#define COLOR_DETECTION_PERCENT_TO_Y(percent) \
    ((uint16)(((uint32)SCC8660_H * (uint32)(percent) + 50u) / 100u))

/* Detect cones only in the lower half of the camera image. This excludes the
 * upper-background region while retaining the complete image width.
 */
#define COLOR_DETECTION_ROI_X_MIN            \
    (COLOR_DETECTION_PERCENT_TO_X(TC4_CFG_COLOR_ROI_LEFT_PERCENT))
#define COLOR_DETECTION_ROI_X_MAX            \
    (COLOR_DETECTION_PERCENT_TO_X(TC4_CFG_COLOR_ROI_RIGHT_PERCENT) - 1u)
#define COLOR_DETECTION_ROI_Y_MIN            \
    (COLOR_DETECTION_PERCENT_TO_Y(TC4_CFG_COLOR_ROI_TOP_PERCENT))
#define COLOR_DETECTION_ROI_Y_MAX            (SCC8660_H - 1u)

/* The old code used SWAPBYTE() before RGB565 decoding.
 * Keep this enabled unless the camera DATA_FORMAT / DMA byte order is changed.
 */
#define COLOR_DETECTION_SWAP_RGB565_BYTES    (TC4_CFG_COLOR_SWAP_RGB565_BYTES)

/* Number of image rows used to estimate cone-ground contact x position. */
#define COLOR_DETECTION_BOTTOM_BAND_ROWS     (TC4_CFG_COLOR_BOTTOM_BAND_ROWS)
#define COLOR_DETECTION_BOTTOM_MIN_PIXELS    (TC4_CFG_COLOR_BOTTOM_MIN_PIXELS)

/* White sign-board detection. A pixel is white only when every channel is
 * bright and its chroma is low.  The component-area limit is evaluated in the
 * same lower-half ROI used for cone detection. */
#define COLOR_DETECTION_WHITE_MIN_CHANNEL    (TC4_CFG_WHITE_MIN_CHANNEL)
#define COLOR_DETECTION_WHITE_MAX_CHROMA     (TC4_CFG_WHITE_MAX_CHROMA)
#define COLOR_DETECTION_WHITE_MIN_AREA       (TC4_CFG_WHITE_MIN_AREA)
#define COLOR_DETECTION_WHITE_ROI_Y_MIN      \
    (COLOR_DETECTION_PERCENT_TO_Y(TC4_CFG_WHITE_ROI_TOP_PERCENT))
#define COLOR_DETECTION_WHITE_ROI_Y_MAX      (SCC8660_H - 1u)

/* Copy SCC8660 DMA image before processing.
 * This avoids processing a buffer that can be overwritten by the next frame.
 * Cost: SCC8660_W*SCC8660_H*2 bytes extra RAM and one frame memcpy.
 */
#define COLOR_DETECTION_COPY_CAMERA_FRAME    (TC4_CFG_COLOR_COPY_CAMERA_FRAME)

typedef enum
{
    COLOR_DETECTION_NONE   = 0,
    COLOR_DETECTION_RED    = 1,
    COLOR_DETECTION_YELLOW = 2
} ColorDetectionColor;

typedef struct
{
    /* H/S/L range, all 0..240. h_min > h_max means circular Hue interval. */
    uint8 h_min;
    uint8 h_max;
    uint8 s_min;
    uint8 s_max;
    uint8 l_min;
    uint8 l_max;

    /* Blob filters. */
    uint16 area_min;
    uint16 area_max;
    uint16 width_min;
    uint16 height_min;

    /* fill = area / bbox_area, scaled by 1000. */
    uint16 fill_min_permille;

    /* aspect = height / width, scaled by 1000. */
    uint16 aspect_min_permille;
    uint16 aspect_max_permille;
} ColorDetectionThreshold;

typedef struct
{
    ColorDetectionColor color;

    /* Raw distorted pixel of cone-ground contact point. */
    uint16 pixel_u;
    uint16 pixel_v;

    /* Bounding box in raw image. */
    uint16 xmin;
    uint16 ymin;
    uint16 xmax;
    uint16 ymax;
    uint16 width;
    uint16 height;
    uint16 area;

    uint16 fill_permille;
    uint16 aspect_permille;

    /* Vehicle/ground coordinate returned by CameraIPM. */
    float ground_x;
    float ground_y;
    uint8 ipm_valid;
} ColorDetectionCone;

typedef struct
{
    uint8 red_count;
    uint8 yellow_count;

    /* Set when one connected, neutral-white region reaches WHITE_MIN_AREA. */
    uint8 large_white_detected;
    uint16 largest_white_component_area;

    /* Bottom-center contact of the largest valid white sign-board region.
     * IPM uses this ground-plane point; the angle is atan2(y, x), relative to
     * the vehicle forward (+X) axis, in radians. */
    uint16 white_sign_pixel_u;
    uint16 white_sign_pixel_v;
    float white_sign_ground_x;
    float white_sign_ground_y;
    float white_sign_angle_error_rad;
    uint8 white_sign_ipm_valid;

    ColorDetectionCone red[COLOR_DETECTION_MAX_CONES];
    ColorDetectionCone yellow[COLOR_DETECTION_MAX_CONES];

    /* Debug/performance counters. */
    uint32 roi_pixels;
    uint32 fast_rejected_pixels;
    uint32 hsl_conversions;
    uint32 white_candidate_pixels;
    uint16 connected_components;
    uint16 rejected_components;
    uint16 white_connected_components;
} ColorDetectionResult;

/* Runtime-adjustable thresholds. */
extern ColorDetectionThreshold g_color_detection_red_threshold;
extern ColorDetectionThreshold g_color_detection_yellow_threshold;

/* Clear result/state. No camera reconfiguration is performed here. */
void ColorDetection_Init(void);

/* Process an already stable RGB565 frame. */
uint8 ColorDetection_ProcessImage(
    const uint16 image[SCC8660_H][SCC8660_W],
    ColorDetectionResult *result);

/* Safe convenience wrapper for the SCC8660 DMA buffer.
 * Returns 1 when a completed frame was consumed and processed, otherwise 0.
 */
uint8 ColorDetection_ProcessCameraFrame(ColorDetectionResult *result);

/* Optional helper: set a manually tuned white-balance value.
 * Driver-valid manual WB range is documented as 0x65..0xA0.
 * Passing 0 re-enables automatic white balance.
 */
uint8 ColorDetection_SetWhiteBalance(uint16 wb_value);

#ifdef __cplusplus
}
#endif

#endif /* CODE_COLORDETECTION_H_ */
