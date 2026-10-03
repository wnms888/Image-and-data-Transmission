/*
 * CameraIPM: one calibration, two complementary mappings.
 * RawPixelToGround is point undistortion followed by H (for ground contacts).
 * RenderIpmRgb565 is reverse IMAGE sampling: H inverse -> forward distortion
 * -> source-size scaling -> nearest pixel. It does NOT undistort twice.
 * Both image processing branches receive the same stable ORIGINAL frame.
 */
#ifndef CODE_CAMERAIPM_H_
#define CODE_CAMERAIPM_H_
#include "Header.h"
#include "TC4ImageProcessingConfig.h"
#if TC4_IMAGE_PROCESSING_CONFIG_VERSION != 2u
#error Export and replace the complete v2 TC4ImageProcessingConfig.h
#endif
#if SCC8660_W < 1 || SCC8660_H < 1 || SCC8660_W > 65535 || SCC8660_H > 65535
#error Camera dimensions must be between 1 and 65535
#endif
#ifdef __cplusplus
extern "C" {
#endif

#define CAMERA_REFERENCE_WIDTH  TC4_IPM_REFERENCE_WIDTH
#define CAMERA_REFERENCE_HEIGHT TC4_IPM_REFERENCE_HEIGHT
#define CAMERA_RADIAL_COUNT     TC4_IPM_RADIAL_COUNT
#define CAMERA_IPM_OUTPUT_WIDTH TC4_IPM_VIEW_OUTPUT_WIDTH
#define CAMERA_IPM_OUTPUT_HEIGHT TC4_IPM_VIEW_OUTPUT_HEIGHT

/* Binary64, as on the host. Do not add f suffixes or enable fast-math:
 * prematurely rounding calibration to float changes nearest-pixel choices. */
static const double CAMERA_FX = TC4_IPM_FX;
static const double CAMERA_FY = TC4_IPM_FY;
static const double CAMERA_CX = TC4_IPM_CX;
static const double CAMERA_CY = TC4_IPM_CY;
static const double CAMERA_SKEW = TC4_IPM_SKEW;
static const double CAMERA_P1 = TC4_IPM_P1;
static const double CAMERA_P2 = TC4_IPM_P2;
static const double CAMERA_K[6] = {
    TC4_IPM_K1, TC4_IPM_K2, TC4_IPM_K3,
    TC4_IPM_K4, TC4_IPM_K5, TC4_IPM_K6
};
static const double CAMERA_IPM_H[9] = {
    TC4_IPM_H11, TC4_IPM_H12, TC4_IPM_H13,
    TC4_IPM_H21, TC4_IPM_H22, TC4_IPM_H23,
    TC4_IPM_H31, TC4_IPM_H32, TC4_IPM_H33
};
#if CAMERA_REFERENCE_WIDTH < 1 || CAMERA_REFERENCE_HEIGHT < 1
#error Camera calibration reference dimensions must be positive
#endif
#if CAMERA_IPM_OUTPUT_WIDTH < 8 || CAMERA_IPM_OUTPUT_HEIGHT < 8 || \
    CAMERA_IPM_OUTPUT_WIDTH > 65535 || CAMERA_IPM_OUTPUT_HEIGHT > 65535
#error IPM output dimensions must be between 8 and 65535
#endif
#if CAMERA_RADIAL_COUNT != 2 && CAMERA_RADIAL_COUNT != 3 && CAMERA_RADIAL_COUNT != 6
#error Radial coefficient count must be 2, 3 or 6
#endif

/* Retained float output ABI for existing ground-contact users. */
typedef struct { float x; float y; } CameraGround2f;
typedef struct {
    double near_x, far_x, half_width_y;
    uint16 output_width, output_height;
} CameraIpmViewConfig;

/* Raw distorted SCC8660 pixel -> ground in calibration units.
 * Eight fixed-point undistortion iterations. Returns false on NULL, invalid
 * calibration, a singular projection, or non-finite results. No allocation. */
bool Camera_RawPixelToGround(float ud, float vd, CameraGround2f *ground);

/* Ground -> raw distorted SCC8660 pixel, fractional coordinates (no rounding).
 * Legacy float interface for individual points, NOT the image sampling path. */
bool Camera_GroundToRawPixel(float ground_x, float ground_y, float *ud, float *vd);

/* Copy the compile-time exported view; NULL is a no-op. */
void Camera_GetIpmViewConfig(CameraIpmViewConfig *config);

/* Full bird's-eye image using exactly the host's orientation and sampling:
 * top row = far X, bottom = near X; left = +Y, right = -Y.
 * Nearest sampling uses ties-to-even (Python round), invalid pixels are black.
 * Output WORDS retain the original byte/channel layout, not native RGB888.
 * Caller owns non-overlapping input/output buffers and keeps input unchanged
 * until return. Output size = configured width * height * 2 bytes.
 * No frame-sized scratch RAM is allocated; optional renderer costs binary64
 * calculations per output pixel. Existing contact detection does not call it.
 * Returns false on NULL/invalid calibration; invalid calibration clears output.
 * Buffers should be declared with CAMERA_IPM_OUTPUT_* so header-only changes
 * resize them automatically. Never use this output as colour-detector input. */
bool Camera_RenderIpmRgb565(
    const uint16 image[SCC8660_H][SCC8660_W],
    uint16 output[CAMERA_IPM_OUTPUT_HEIGHT][CAMERA_IPM_OUTPUT_WIDTH]);

#ifdef __cplusplus
}
#endif
#endif /* CODE_CAMERAIPM_H_ */
