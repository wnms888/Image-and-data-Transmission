/*
 * CameraIPM.c
 *
 * Runtime:
 *   distorted raw pixel (u_d, v_d)
 *       -> iterative point undistortion
 *       -> undistorted pixel (u_u, v_u)
 *       -> 3x3 homography
 *       -> vehicle-ground coordinate (X,Y)
 */

#include "Header.h"

static bool Camera_UndistortPoint(
    float ud,
    float vd,
    float *uu,
    float *vu)
{
    float x;
    float y;
    float xd;
    float yd;
    uint32_t iter;

    if ((uu == 0) || (vu == 0))
    {
        return false;
    }

    if ((fabsf(CAMERA_FX) < 1.0e-12f) ||
        (fabsf(CAMERA_FY) < 1.0e-12f))
    {
        return false;
    }

    yd = (vd - CAMERA_CY) / CAMERA_FY;
    xd = (ud - CAMERA_CX - CAMERA_SKEW * yd) / CAMERA_FX;

    x = xd;
    y = yd;

    for (iter = 0u; iter < 8u; ++iter)
    {
        const float x2 = x * x;
        const float y2 = y * y;
        const float r2 = x2 + y2;
        const float r4 = r2 * r2;
        const float r6 = r4 * r2;

        const float k1 = CAMERA_K[0];
        const float k2 = CAMERA_K[1];
        const float k3 =
            (CAMERA_RADIAL_COUNT >= 3u) ? CAMERA_K[2] : 0.0f;
        const float k4 =
            (CAMERA_RADIAL_COUNT >= 6u) ? CAMERA_K[3] : 0.0f;
        const float k5 =
            (CAMERA_RADIAL_COUNT >= 6u) ? CAMERA_K[4] : 0.0f;
        const float k6 =
            (CAMERA_RADIAL_COUNT >= 6u) ? CAMERA_K[5] : 0.0f;

        const float radial_num =
            1.0f + k1 * r2 + k2 * r4 + k3 * r6;

        const float radial_den =
            1.0f + k4 * r2 + k5 * r4 + k6 * r6;

        float radial;
        float delta_x;
        float delta_y;

        if ((fabsf(radial_num) < 1.0e-12f) ||
            (fabsf(radial_den) < 1.0e-12f))
        {
            return false;
        }

        radial = radial_num / radial_den;

        delta_x =
            2.0f * CAMERA_P1 * x * y +
            CAMERA_P2 * (r2 + 2.0f * x2);

        delta_y =
            CAMERA_P1 * (r2 + 2.0f * y2) +
            2.0f * CAMERA_P2 * x * y;

        x = (xd - delta_x) / radial;
        y = (yd - delta_y) / radial;

        if ((!isfinite(x)) || (!isfinite(y)))
        {
            return false;
        }
    }

    *uu = CAMERA_FX * x + CAMERA_SKEW * y + CAMERA_CX;
    *vu = CAMERA_FY * y + CAMERA_CY;

    return isfinite(*uu) && isfinite(*vu);
}

static bool Camera_IPMProjectUndistortedPoint(
    float uu,
    float vu,
    CameraGround2f *ground)
{
    float w;

    if (ground == 0)
    {
        return false;
    }

    w =
        CAMERA_IPM_H[6] * uu +
        CAMERA_IPM_H[7] * vu +
        CAMERA_IPM_H[8];

    if (fabsf(w) < 1.0e-10f)
    {
        return false;
    }

    ground->x =
        (CAMERA_IPM_H[0] * uu +
         CAMERA_IPM_H[1] * vu +
         CAMERA_IPM_H[2]) / w;

    ground->y =
        (CAMERA_IPM_H[3] * uu +
         CAMERA_IPM_H[4] * vu +
         CAMERA_IPM_H[5]) / w;

    return isfinite(ground->x) && isfinite(ground->y);
}

bool Camera_RawPixelToGround(
    float ud,
    float vd,
    CameraGround2f *ground)
{
    float uu = 0.0f;
    float vu = 0.0f;

    if (!Camera_UndistortPoint(ud, vd, &uu, &vu))
    {
        return false;
    }

    return Camera_IPMProjectUndistortedPoint(uu, vu, ground);
}
