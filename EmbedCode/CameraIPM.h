/*
 * CameraIPM.h
 *
 * Raw distorted pixel -> point undistortion -> Homography -> vehicle ground.
 */

#ifndef CODE_CAMERAIPM_H_
#define CODE_CAMERAIPM_H_

#include "Header.h"
#include "TC4ImageProcessingConfig.h"

#ifdef __cplusplus
extern "C" {
#endif

#define CAMERA_REFERENCE_WIDTH  TC4_IPM_REFERENCE_WIDTH
#define CAMERA_REFERENCE_HEIGHT TC4_IPM_REFERENCE_HEIGHT
#define CAMERA_RADIAL_COUNT     TC4_IPM_RADIAL_COUNT

/* Values are defined exclusively by TC4ImageProcessingConfig.h so exporting
 * a host configuration and replacing that file updates this C module too. */
static const float CAMERA_FX   = TC4_IPM_FX;
static const float CAMERA_FY   = TC4_IPM_FY;
static const float CAMERA_CX   = TC4_IPM_CX;
static const float CAMERA_CY   = TC4_IPM_CY;
static const float CAMERA_SKEW = TC4_IPM_SKEW;

static const float CAMERA_K[6] = {
    TC4_IPM_K1, TC4_IPM_K2, TC4_IPM_K3,
    TC4_IPM_K4, TC4_IPM_K5, TC4_IPM_K6
};

static const float CAMERA_P1 = TC4_IPM_P1;
static const float CAMERA_P2 = TC4_IPM_P2;

/* H maps UNDISTORTED pixel [u v 1]^T to vehicle ground [X Y 1]^T. */
static const float CAMERA_IPM_H[9] = {
    TC4_IPM_H11, TC4_IPM_H12, TC4_IPM_H13,
    TC4_IPM_H21, TC4_IPM_H22, TC4_IPM_H23,
    TC4_IPM_H31, TC4_IPM_H32, TC4_IPM_H33
};

typedef struct
{
    float x;
    float y;
} CameraGround2f;

/* Bird's-eye output rectangle configured by the host.  The existing detector
 * uses Camera_RawPixelToGround(); image renderers can use this descriptor and
 * Camera_GroundToRawPixel() without duplicating the exported constants. */
typedef struct
{
    float near_x;
    float far_x;
    float half_width_y;
    uint16 output_width;
    uint16 output_height;
} CameraIpmViewConfig;

/* Public single-call interface used by ColorDetection.
 * Input : raw/distorted SCC8660 pixel.
 * Output: vehicle-ground coordinate defined by MATLAB calibration.
 */
bool Camera_RawPixelToGround(
    float ud,
    float vd,
    CameraGround2f *ground);

/* Reverse mapping used by an embedded bird's-eye renderer. */
bool Camera_GroundToRawPixel(
    float ground_x,
    float ground_y,
    float *ud,
    float *vd);

/* Copy the host-exported bird's-eye range and output dimensions. */
void Camera_GetIpmViewConfig(CameraIpmViewConfig *config);

#ifdef __cplusplus
}
#endif

#endif /* CODE_CAMERAIPM_H_ */
