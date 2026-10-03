/* Host-equivalent reverse image sampling, plus ground-contact point mapping.
 * Configuration is read only from TC4ImageProcessingConfig.h via CameraIPM.h.
 * Binary64 operation order deliberately mirrors HostApp/image_processing.py.
 */
#include "CameraIPM.h"
#include <math.h>
#include <string.h>

typedef char CameraRequiresBinary64Double[sizeof(double) == 8 ? 1 : -1];
#ifdef __FAST_MATH__
#error Disable fast-math to preserve host-equivalent IPM sampling
#endif

static bool camera_inverse_h(double inverse[9])
{
    const double a=CAMERA_IPM_H[0], b=CAMERA_IPM_H[1], c=CAMERA_IPM_H[2];
    const double d=CAMERA_IPM_H[3], e=CAMERA_IPM_H[4], f=CAMERA_IPM_H[5];
    const double g=CAMERA_IPM_H[6], h=CAMERA_IPM_H[7], i=CAMERA_IPM_H[8];
    double determinant;
    uint16 n;
    inverse[0]=e*i-f*h; inverse[1]=c*h-b*i; inverse[2]=b*f-c*e;
    inverse[3]=f*g-d*i; inverse[4]=a*i-c*g; inverse[5]=c*d-a*f;
    inverse[6]=d*h-e*g; inverse[7]=b*g-a*h; inverse[8]=a*e-b*d;
    determinant = a*inverse[0]+b*inverse[3]+c*inverse[6];
    if (!isfinite(determinant) || fabs(determinant)<1.0e-12) return false;
    for (n=0u; n<9u; ++n) inverse[n] /= determinant;
    return true;
}

/* Forward distortion in normalized camera coordinates. */
static bool camera_distort(double x, double y, double *ud, double *vd)
{
    const double r2=x*x+y*y, r4=r2*r2;
#if CAMERA_RADIAL_COUNT >= 3u
    const double r6=r4*r2;
#endif
    double numerator=1.0+CAMERA_K[0]*r2+CAMERA_K[1]*r4;
    double denominator=1.0, radial, xd, yd;
#if CAMERA_RADIAL_COUNT >= 3u
    numerator += CAMERA_K[2]*r6;
#endif
#if CAMERA_RADIAL_COUNT >= 6u
    denominator += CAMERA_K[3]*r2+CAMERA_K[4]*r4+CAMERA_K[5]*r6;
#endif
    if (fabs(denominator)<1.0e-12) return false;
    radial=numerator/denominator;
    xd=x*radial+2.0*CAMERA_P1*x*y+CAMERA_P2*(r2+2.0*x*x);
    yd=y*radial+CAMERA_P1*(r2+2.0*y*y)+2.0*CAMERA_P2*x*y;
    *ud=CAMERA_FX*xd+CAMERA_SKEW*yd+CAMERA_CX;
    *vd=CAMERA_FY*yd+CAMERA_CY;
    return isfinite(*ud) && isfinite(*vd);
}

static bool camera_ground_to_reference(
    double gx, double gy, const double inverse[9], double *ud, double *vd)
{
    const double w=inverse[6]*gx+inverse[7]*gy+inverse[8];
    double uu, vu, x, y;
    if (fabs(w)<1.0e-12 || fabs(CAMERA_FX)<1.0e-12 ||
        fabs(CAMERA_FY)<1.0e-12) return false;
    uu=(inverse[0]*gx+inverse[1]*gy+inverse[2])/w;
    vu=(inverse[3]*gx+inverse[4]*gy+inverse[5])/w;
    y=(vu-CAMERA_CY)/CAMERA_FY;
    x=(uu-CAMERA_CX-CAMERA_SKEW*y)/CAMERA_FX;
    return camera_distort(x, y, ud, vd);
}

/* Unlike C round(), Python round() selects the even integer at exact halves.
 * Keep a double until after bounds checking to avoid integer overflow. */
static double camera_round_even(double value)
{
    const double lower=floor(value), fraction=value-lower;
    if (fraction<0.5) return lower;
    if (fraction>0.5) return lower+1.0;
    return fmod(lower, 2.0)==0.0 ? lower : lower+1.0;
}

bool Camera_RenderIpmRgb565(
    const uint16 image[SCC8660_H][SCC8660_W],
    uint16 output[CAMERA_IPM_OUTPUT_HEIGHT][CAMERA_IPM_OUTPUT_WIDTH])
{
    double inverse[9];
    const double uscale=(double)SCC8660_W/CAMERA_REFERENCE_WIDTH;
    const double vscale=(double)SCC8660_H/CAMERA_REFERENCE_HEIGHT;
    uint16 oy, ox;
    if (image==0 || output==0) return false;
    if (TC4_IPM_VIEW_FAR_X<=TC4_IPM_VIEW_NEAR_X ||
        TC4_IPM_VIEW_HALF_WIDTH_Y<=0.0 ||
        !isfinite(TC4_IPM_VIEW_NEAR_X) || !isfinite(TC4_IPM_VIEW_FAR_X) ||
        !isfinite(TC4_IPM_VIEW_HALF_WIDTH_Y) ||
        fabs(CAMERA_FX)<1.0e-12 || fabs(CAMERA_FY)<1.0e-12 ||
        !camera_inverse_h(inverse))
    {
        memset(output, 0, (size_t)CAMERA_IPM_OUTPUT_HEIGHT*CAMERA_IPM_OUTPUT_WIDTH*sizeof(uint16));
        return false;
    }
    for (oy=0u; oy<CAMERA_IPM_OUTPUT_HEIGHT; ++oy)
    {
        const double fraction=(double)oy/(CAMERA_IPM_OUTPUT_HEIGHT-1u);
        const double gx=TC4_IPM_VIEW_FAR_X-fraction*(TC4_IPM_VIEW_FAR_X-TC4_IPM_VIEW_NEAR_X);
        for (ox=0u; ox<CAMERA_IPM_OUTPUT_WIDTH; ++ox)
        {
            const double yf=(double)ox/(CAMERA_IPM_OUTPUT_WIDTH-1u);
            const double gy=TC4_IPM_VIEW_HALF_WIDTH_Y-yf*(2.0*TC4_IPM_VIEW_HALF_WIDTH_Y);
            double u, v;
            output[oy][ox]=0u;
            if (!camera_ground_to_reference(gx, gy, inverse, &u, &v)) continue;
            u=camera_round_even(u*uscale); v=camera_round_even(v*vscale);
            if (u>=0.0 && u<SCC8660_W && v>=0.0 && v<SCC8660_H)
                output[oy][ox]=image[(uint16)v][(uint16)u];
        }
    }
    return true;
}

bool Camera_GroundToRawPixel(float gx, float gy, float *ud, float *vd)
{
    double inverse[9], u, v;
    if (ud==0 || vd==0 || !camera_inverse_h(inverse) ||
        !camera_ground_to_reference(gx, gy, inverse, &u, &v)) return false;
    *ud=(float)(u*((double)SCC8660_W/CAMERA_REFERENCE_WIDTH));
    *vd=(float)(v*((double)SCC8660_H/CAMERA_REFERENCE_HEIGHT));
    return isfinite(*ud) && isfinite(*vd);
}

bool Camera_RawPixelToGround(float ud, float vd, CameraGround2f *ground)
{
    double xd, yd, x, y, uu, vu, w;
    uint16 iter;
    if (ground==0 || fabs(CAMERA_FX)<1.0e-12 || fabs(CAMERA_FY)<1.0e-12) return false;
    yd=(vd*((double)CAMERA_REFERENCE_HEIGHT/SCC8660_H)-CAMERA_CY)/CAMERA_FY;
    xd=(ud*((double)CAMERA_REFERENCE_WIDTH/SCC8660_W)-CAMERA_CX-CAMERA_SKEW*yd)/CAMERA_FX;
    x=xd; y=yd;
    for (iter=0u; iter<8u; ++iter)
    {
        const double r2=x*x+y*y, r4=r2*r2;
#if CAMERA_RADIAL_COUNT >= 3u
        const double r6=r4*r2;
#endif
        double numerator=1.0+CAMERA_K[0]*r2+CAMERA_K[1]*r4;
        double denominator=1.0, radial, dx, dy;
#if CAMERA_RADIAL_COUNT >= 3u
        numerator += CAMERA_K[2]*r6;
#endif
#if CAMERA_RADIAL_COUNT >= 6u
        denominator += CAMERA_K[3]*r2+CAMERA_K[4]*r4+CAMERA_K[5]*r6;
#endif
        if (fabs(numerator)<1.0e-12 || fabs(denominator)<1.0e-12) return false;
        radial=numerator/denominator;
        dx=2.0*CAMERA_P1*x*y+CAMERA_P2*(r2+2.0*x*x);
        dy=CAMERA_P1*(r2+2.0*y*y)+2.0*CAMERA_P2*x*y;
        x=(xd-dx)/radial; y=(yd-dy)/radial;
        if (!isfinite(x) || !isfinite(y)) return false;
    }
    uu=CAMERA_FX*x+CAMERA_SKEW*y+CAMERA_CX; vu=CAMERA_FY*y+CAMERA_CY;
    w=CAMERA_IPM_H[6]*uu+CAMERA_IPM_H[7]*vu+CAMERA_IPM_H[8];
    if (fabs(w)<1.0e-12) return false;
    ground->x=(float)((CAMERA_IPM_H[0]*uu+CAMERA_IPM_H[1]*vu+CAMERA_IPM_H[2])/w);
    ground->y=(float)((CAMERA_IPM_H[3]*uu+CAMERA_IPM_H[4]*vu+CAMERA_IPM_H[5])/w);
    return isfinite(ground->x) && isfinite(ground->y);
}

void Camera_GetIpmViewConfig(CameraIpmViewConfig *config)
{
    if (config==0) return;
    config->near_x=TC4_IPM_VIEW_NEAR_X;
    config->far_x=TC4_IPM_VIEW_FAR_X;
    config->half_width_y=TC4_IPM_VIEW_HALF_WIDTH_Y;
    config->output_width=(uint16)CAMERA_IPM_OUTPUT_WIDTH;
    config->output_height=(uint16)CAMERA_IPM_OUTPUT_HEIGHT;
}
