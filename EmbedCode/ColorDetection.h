/*
 * ColorDetection.h
 *
 * Configurable, ordered colour detection for SCC8660 RGB565 images.
 *
 * Pipeline:
 *   RGB565 -> HSL -> first matching configured threshold
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
#if TC4_IMAGE_PROCESSING_CONFIG_VERSION != 2u
#error Export and replace the complete v2 TC4ImageProcessingConfig.h
#endif

#ifdef __cplusplus
extern "C" {
#endif

#define COLOR_DETECTION_MAX_CONES            (TC4_CFG_COLOR_MAX_CONES)
#define COLOR_DETECTION_COLOR_COUNT          (TC4_CFG_COLOR_COUNT)

/* Host and target both express ROI as percentages of the current frame.  The
 * +50 form is round-to-nearest for the SCC8660 dimensions and matches the
 * host preview's pixel conversion. */
#define COLOR_DETECTION_PERCENT_TO_X(percent) \
    ((uint16)(((uint32)SCC8660_W * (uint32)(percent) + 50u) / 100u))
#define COLOR_DETECTION_PERCENT_TO_Y(percent) \
    ((uint16)(((uint32)SCC8660_H * (uint32)(percent) + 50u) / 100u))

/* Configured ROI: left inclusive, right EXCLUSIVE. An empty rounded ROI
 * is legal and detects nothing; never subtract 1 from an unsigned boundary. */
#define COLOR_DETECTION_ROI_X_MIN            \
    (COLOR_DETECTION_PERCENT_TO_X(TC4_CFG_COLOR_ROI_LEFT_PERCENT))
#define COLOR_DETECTION_ROI_X_END            \
    (COLOR_DETECTION_PERCENT_TO_X(TC4_CFG_COLOR_ROI_RIGHT_PERCENT))
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

/* Additional embedded-only white-board metadata, AFTER common colour
 * detection. It has its OWN vertical ROI and never changes the preview mask. */
#define COLOR_DETECTION_WHITE_MIN_CHANNEL    (TC4_CFG_WHITE_MIN_CHANNEL)
#define COLOR_DETECTION_WHITE_MAX_CHROMA     (TC4_CFG_WHITE_MAX_CHROMA)
#define COLOR_DETECTION_WHITE_MIN_AREA       (TC4_CFG_WHITE_MIN_AREA)
#define COLOR_DETECTION_WHITE_ROI_Y_MIN      \
    (COLOR_DETECTION_PERCENT_TO_Y(TC4_CFG_WHITE_ROI_TOP_PERCENT))
#define COLOR_DETECTION_WHITE_ROI_Y_MAX      (SCC8660_H - 1u)

/* Optional camera snapshot, SCC8660_W*SCC8660_H*2 extra bytes plus memcpy.
 * The caller still needs driver ownership/locking during the copy. A memcpy
 * alone cannot prevent concurrent DMA writes or guarantee a coherent frame.
 */
#define COLOR_DETECTION_COPY_CAMERA_FRAME    (TC4_CFG_COLOR_COPY_CAMERA_FRAME)

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
    /* 0-based index into g_color_detection_thresholds and result->colours. */
    uint16 color_index;

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
    uint32 area;

    uint16 fill_permille;
    uint16 aspect_permille;

    /* Vehicle/ground coordinate returned by CameraIPM. */
    float ground_x;
    float ground_y;
    uint8 ipm_valid;
} ColorDetectionCone;

typedef struct
{
    uint16 count;
    ColorDetectionCone cones[COLOR_DETECTION_MAX_CONES];
} ColorDetectionGroup;

typedef struct
{
    ColorDetectionGroup colours[COLOR_DETECTION_COLOR_COUNT];

    /* Set when one connected, neutral-white region reaches WHITE_MIN_AREA. */
    uint8 large_white_detected;
    uint32 largest_white_component_area;

    /* Bottom-center contact of the largest valid white sign-board region.
     * IPM uses this ground-plane point; the angle is atan2(y, x), relative to
     * the vehicle forward (+X) axis, in radians. */
    uint16 white_sign_pixel_u;
    uint16 white_sign_pixel_v;
    float white_sign_ground_x;
    float white_sign_ground_y;
    float white_sign_angle_error_rad;
    uint8 white_sign_ipm_valid;

    /* Debug/performance counters. */
    uint32 roi_pixels;
    uint32 fast_rejected_pixels; /* Reserved, always zero: no blue rejection. */
    uint32 hsl_conversions;
    uint32 white_candidate_pixels;
    uint32 connected_components;
    uint32 rejected_components;
    uint32 white_connected_components;
} ColorDetectionResult;

/* Read-only tables from the single exported header; index is 0-based.
 * Unsized declaration permits compile-time validation of initializer count. */
extern const ColorDetectionThreshold g_color_detection_thresholds[];
extern const uint8 g_color_detection_preview_rgb[][3];

/* Bounds-checked access; returns NULL for an out-of-range colour index. */
const ColorDetectionThreshold *ColorDetection_GetThreshold(uint16 color_index);

/* Decode a camera-memory WORD and convert to HSL 0..240 using the configured
 * byte/channel order. Returns 0 for any NULL output, otherwise 1. */
uint8 ColorDetection_Rgb565ToHsl(uint16 raw, uint8 *h, uint8 *s, uint8 *l);

/* Clear result/state. No camera reconfiguration is performed here. */
void ColorDetection_Init(void);

/* Process an already stable ORIGINAL RGB565 frame. Clears result each call;
 * returns total accepted components, 0 for invalid pointers or no matches.
 * Order: ROI -> RGB888 -> integer HSL -> first matching rule -> 4-connected
 * components -> geometry -> per-colour count cap -> ground-contact metadata
 * -> independent white-board metadata. No IPM image is an input here.
 * Non-reentrant: use only one CPU/task at a time (shared static workspace).
 * Keep input stable until return; result is owned by caller. */
uint32 ColorDetection_ProcessImage(
    const uint16 image[SCC8660_H][SCC8660_W],
    ColorDetectionResult *result);

/* Same processing, optionally paints the HOST-EQUIVALENT RGB888 result:
 * accepted pixels = exported preview colour, bbox border = white, rest black.
 * NULL preview saves painting cost and frame-sized output RAM. Non-NULL output
 * must not overlap source/result and requires W*H*3 bytes owned by caller.
 * This is a RGB888 image, NOT a packet accepted by SubmitRgb565Frame(). */
uint32 ColorDetection_ProcessImageWithPreview(
    const uint16 image[SCC8660_H][SCC8660_W],
    ColorDetectionResult *result,
    uint8 preview[SCC8660_H][SCC8660_W][3]);

/* Convenience wrapper for SCC8660 DMA buffer, with ownership precondition:
 * ensure the driver cannot overwrite the completed frame while processing,
 * or while taking the optional copy. Prefer ProcessImage on your stable frame.
 * This function consumes/clears scc8660_finish_flag.
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
