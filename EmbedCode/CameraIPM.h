/*
 * CameraIPM.h
 *
 * Raw distorted pixel -> point undistortion -> Homography -> vehicle ground.
 */

#ifndef CODE_CAMERAIPM_H_
#define CODE_CAMERAIPM_H_

#include "Header.h"

#ifdef __cplusplus
extern "C" {
#endif

#define CAMERA_RADIAL_COUNT 2u

/* Calibration generated from MATLAB. */
static const float CAMERA_FX   = 86.0473887f;
static const float CAMERA_FY   = 88.6329816f;
static const float CAMERA_CX   = 82.4574101f;
static const float CAMERA_CY   = 66.3210952f;
static const float CAMERA_SKEW = 0.0f;

static const float CAMERA_K[6] = {
    0.018489635f, -0.028478149f, 0.0f, 0.0f, 0.0f, 0.0f
};

static const float CAMERA_P1 = 0.0f;
static const float CAMERA_P2 = 0.0f;

/* H maps UNDISTORTED pixel [u v 1]^T to vehicle ground [X Y 1]^T. */
static const float CAMERA_IPM_H[9] = {
    0.00126378934f, 0.176208985f, -48.1200764f,
    0.447881955f, 0.00593926103f, -36.8896207f,
    0.0173753584f, -1.03867922f, 1.0f
};

typedef struct
{
    float x;
    float y;
} CameraGround2f;

/* Public single-call interface used by ColorDetection.
 * Input : raw/distorted SCC8660 pixel.
 * Output: vehicle-ground coordinate defined by MATLAB calibration.
 */
bool Camera_RawPixelToGround(
    float ud,
    float vd,
    CameraGround2f *ground);

#ifdef __cplusplus
}
#endif

#endif /* CODE_CAMERAIPM_H_ */
