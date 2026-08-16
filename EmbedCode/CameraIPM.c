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
#include "CameraIPM.h"

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

static bool Camera_GroundToUndistortedPixel(
    float ground_x,
    float ground_y,
    float *uu,
    float *vu)
{
    const float a = CAMERA_IPM_H[0];
    const float b = CAMERA_IPM_H[1];
    const float c = CAMERA_IPM_H[2];
    const float d = CAMERA_IPM_H[3];
    const float e = CAMERA_IPM_H[4];
    const float f = CAMERA_IPM_H[5];
    const float g = CAMERA_IPM_H[6];
    const float h = CAMERA_IPM_H[7];
    const float i = CAMERA_IPM_H[8];
    const float c00 = e * i - f * h;
    const float c01 = c * h - b * i;
    const float c02 = b * f - c * e;
    const float c10 = f * g - d * i;
    const float c11 = a * i - c * g;
    const float c12 = c * d - a * f;
    const float c20 = d * h - e * g;
    const float c21 = b * g - a * h;
    const float c22 = a * e - b * d;
    const float determinant = a * c00 + b * c10 + c * c20;
    float w;

    if ((uu == 0) || (vu == 0) || (fabsf(determinant) < 1.0e-12f))
    {
        return false;
    }

    w = (c20 * ground_x + c21 * ground_y + c22) / determinant;
    if (fabsf(w) < 1.0e-10f)
    {
        return false;
    }

    *uu = ((c00 * ground_x + c01 * ground_y + c02) / determinant) / w;
    *vu = ((c10 * ground_x + c11 * ground_y + c12) / determinant) / w;
    return isfinite(*uu) && isfinite(*vu);
}

bool Camera_RawPixelToGround(
    float ud,
    float vd,
    CameraGround2f *ground)
{
    float reference_ud;
    float reference_vd;
    float uu = 0.0f;
    float vu = 0.0f;

    if ((CAMERA_REFERENCE_WIDTH == 0u) || (CAMERA_REFERENCE_HEIGHT == 0u) ||
        (SCC8660_W == 0u) || (SCC8660_H == 0u))
    {
        return false;
    }

    /* Host previews scale an incoming image to calibration resolution before
     * applying the model.  Do the same for an SCC8660 frame here. */
    reference_ud = ud * (float)CAMERA_REFERENCE_WIDTH / (float)SCC8660_W;
    reference_vd = vd * (float)CAMERA_REFERENCE_HEIGHT / (float)SCC8660_H;

    if (!Camera_UndistortPoint(reference_ud, reference_vd, &uu, &vu))
    {
        return false;
    }

    return Camera_IPMProjectUndistortedPoint(uu, vu, ground);
}

bool Camera_GroundToRawPixel(
    float ground_x,
    float ground_y,
    float *ud,
    float *vd)
{
    float uu;
    float vu;
    float x;
    float y;
    float r2;
    float r4;
    float r6;
    float radial_num;
    float radial_den;
    float xd;
    float yd;
    float reference_ud;
    float reference_vd;

    if ((ud == 0) || (vd == 0) ||
        (CAMERA_REFERENCE_WIDTH == 0u) || (CAMERA_REFERENCE_HEIGHT == 0u) ||
        (SCC8660_W == 0u) || (SCC8660_H == 0u) ||
        (fabsf(CAMERA_FX) < 1.0e-12f) || (fabsf(CAMERA_FY) < 1.0e-12f) ||
        !Camera_GroundToUndistortedPixel(ground_x, ground_y, &uu, &vu))
    {
        return false;
    }

    y = (vu - CAMERA_CY) / CAMERA_FY;
    x = (uu - CAMERA_CX - CAMERA_SKEW * y) / CAMERA_FX;
    r2 = x * x + y * y;
    r4 = r2 * r2;
    r6 = r4 * r2;
    radial_num = 1.0f + CAMERA_K[0] * r2 + CAMERA_K[1] * r4;
    radial_den = 1.0f;
    if (CAMERA_RADIAL_COUNT >= 3u)
    {
        radial_num += CAMERA_K[2] * r6;
    }
    if (CAMERA_RADIAL_COUNT >= 6u)
    {
        radial_den += CAMERA_K[3] * r2 + CAMERA_K[4] * r4 + CAMERA_K[5] * r6;
    }
    if (fabsf(radial_den) < 1.0e-12f)
    {
        return false;
    }

    radial_num /= radial_den;
    xd = x * radial_num + 2.0f * CAMERA_P1 * x * y +
         CAMERA_P2 * (r2 + 2.0f * x * x);
    yd = y * radial_num + CAMERA_P1 * (r2 + 2.0f * y * y) +
         2.0f * CAMERA_P2 * x * y;
    reference_ud = CAMERA_FX * xd + CAMERA_SKEW * yd + CAMERA_CX;
    reference_vd = CAMERA_FY * yd + CAMERA_CY;
    *ud = reference_ud * (float)SCC8660_W / (float)CAMERA_REFERENCE_WIDTH;
    *vd = reference_vd * (float)SCC8660_H / (float)CAMERA_REFERENCE_HEIGHT;
    return isfinite(*ud) && isfinite(*vd);
}

void Camera_GetIpmViewConfig(CameraIpmViewConfig *config)
{
    if (config == 0)
    {
        return;
    }

    config->near_x = TC4_IPM_VIEW_NEAR_X;
    config->far_x = TC4_IPM_VIEW_FAR_X;
    config->half_width_y = TC4_IPM_VIEW_HALF_WIDTH_Y;
    config->output_width = (uint16)TC4_IPM_VIEW_OUTPUT_WIDTH;
    config->output_height = (uint16)TC4_IPM_VIEW_OUTPUT_HEIGHT;
}
