/*
 * TC4ImageProcessingConfig.h
 *
 * Single-source image-processing configuration shared with the host tool.
 * The host application's "Export configuration (JSON + C)" action replaces
 * this whole file.  After the one-time CameraIPM/ColorDetection integration,
 * copy the exported file here and rebuild; no other C source needs editing.
 */

#ifndef TC4_IMAGE_PROCESSING_CONFIG_H_
#define TC4_IMAGE_PROCESSING_CONFIG_H_

#define TC4_IMAGE_PROCESSING_CONFIG_VERSION       (1u)

/* Camera calibration.  Reference dimensions are the dimensions that were
 * used during calibration, not necessarily the SCC8660 DMA dimensions. */
#define TC4_IPM_REFERENCE_WIDTH                   (160u)
#define TC4_IPM_REFERENCE_HEIGHT                  (128u)
#define TC4_IPM_FX                                (86.0473887f)
#define TC4_IPM_FY                                (88.6329816f)
#define TC4_IPM_CX                                (82.4574101f)
#define TC4_IPM_CY                                (66.3210952f)
#define TC4_IPM_SKEW                              (0.0f)
#define TC4_IPM_RADIAL_COUNT                      (2u)
#define TC4_IPM_K1                                (0.018489635f)
#define TC4_IPM_K2                                (-0.028478149f)
#define TC4_IPM_K3                                (0.0f)
#define TC4_IPM_K4                                (0.0f)
#define TC4_IPM_K5                                (0.0f)
#define TC4_IPM_K6                                (0.0f)
#define TC4_IPM_P1                                (0.0f)
#define TC4_IPM_P2                                (0.0f)

/* Homography: undistorted pixel [u v 1]^T -> ground [X Y 1]^T. */
#define TC4_IPM_H11                               (0.00126378934f)
#define TC4_IPM_H12                               (0.176208985f)
#define TC4_IPM_H13                               (-48.1200764f)
#define TC4_IPM_H21                               (0.447881955f)
#define TC4_IPM_H22                               (0.00593926103f)
#define TC4_IPM_H23                               (-36.8896207f)
#define TC4_IPM_H31                               (0.0173753584f)
#define TC4_IPM_H32                               (-1.03867922f)
#define TC4_IPM_H33                               (1.0f)

/* Bird's-eye view geometry.  Camera_GetIpmViewConfig() exposes this exact
 * host-preview rectangle to embedded renderers or path planners. */
#define TC4_IPM_VIEW_NEAR_X                       (0.2f)
#define TC4_IPM_VIEW_FAR_X                        (2.0f)
#define TC4_IPM_VIEW_HALF_WIDTH_Y                 (0.75f)
#define TC4_IPM_VIEW_OUTPUT_WIDTH                 (240u)
#define TC4_IPM_VIEW_OUTPUT_HEIGHT                (240u)

/* Host-configurable red/yellow detector settings. */
#define TC4_CFG_COLOR_MAX_CONES                   (12u)
#define TC4_CFG_COLOR_ROI_TOP_PERCENT             (25u)
#define TC4_CFG_COLOR_ROI_LEFT_PERCENT            (0u)
#define TC4_CFG_COLOR_ROI_RIGHT_PERCENT           (100u)

#define TC4_CFG_RED_H_MIN                         (230u)
#define TC4_CFG_RED_H_MAX                         (15u)
#define TC4_CFG_RED_S_MIN                         (20u)
#define TC4_CFG_RED_S_MAX                         (240u)
#define TC4_CFG_RED_L_MIN                         (65u)
#define TC4_CFG_RED_L_MAX                         (240u)
#define TC4_CFG_RED_AREA_MIN                      (10u)
#define TC4_CFG_RED_AREA_MAX                      (12000u)
#define TC4_CFG_RED_WIDTH_MIN                     (2u)
#define TC4_CFG_RED_HEIGHT_MIN                    (3u)
#define TC4_CFG_RED_FILL_MIN_PERMILLE             (70u)
#define TC4_CFG_RED_ASPECT_MIN_PERMILLE           (300u)
#define TC4_CFG_RED_ASPECT_MAX_PERMILLE           (5000u)

#define TC4_CFG_YELLOW_H_MIN                      (18u)
#define TC4_CFG_YELLOW_H_MAX                      (42u)
#define TC4_CFG_YELLOW_S_MIN                      (80u)
#define TC4_CFG_YELLOW_S_MAX                      (240u)
#define TC4_CFG_YELLOW_L_MIN                      (70u)
#define TC4_CFG_YELLOW_L_MAX                      (240u)
#define TC4_CFG_YELLOW_AREA_MIN                   (10u)
#define TC4_CFG_YELLOW_AREA_MAX                   (12000u)
#define TC4_CFG_YELLOW_WIDTH_MIN                  (2u)
#define TC4_CFG_YELLOW_HEIGHT_MIN                 (3u)
#define TC4_CFG_YELLOW_FILL_MIN_PERMILLE          (70u)
#define TC4_CFG_YELLOW_ASPECT_MIN_PERMILLE        (300u)
#define TC4_CFG_YELLOW_ASPECT_MAX_PERMILLE        (5000u)

/* Embedded-only safeguards.  They keep the colour pipeline behaviour that
 * existed before host-side tuning was added. */
#define TC4_CFG_COLOR_SWAP_RGB565_BYTES           (1u)
#define TC4_CFG_COLOR_BOTTOM_BAND_ROWS             (3u)
#define TC4_CFG_COLOR_BOTTOM_MIN_PIXELS            (2u)
#define TC4_CFG_COLOR_COPY_CAMERA_FRAME            (0u)
#define TC4_CFG_WHITE_MIN_CHANNEL                  (200u)
#define TC4_CFG_WHITE_MAX_CHROMA                   (40u)
#define TC4_CFG_WHITE_MIN_AREA                     (200u)
#define TC4_CFG_WHITE_ROI_TOP_PERCENT              (25u)

#endif /* TC4_IMAGE_PROCESSING_CONFIG_H_ */
