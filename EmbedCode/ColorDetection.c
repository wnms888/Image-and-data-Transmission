/*
 * ColorDetection.c
 *
 * Common image pipeline matches HostApp.detect_colours:
 * ROI (half-up) -> RGB888 -> integer HSL -> first matching rule
 * -> 4-connected components -> geometry -> count cap -> optional preview.
 * Ground contact and neutral-white metadata are embedded-only extensions.
 * All tunable values/tables come from TC4ImageProcessingConfig.h.
 */

#include "Header.h"
#include "ColorDetection.h"
#include "CameraIPM.h"
#include <string.h>

#define CD_LABEL_NONE       ((CdLabel)0u)
#if COLOR_DETECTION_COLOR_COUNT < 255u
typedef uint8 CdLabel;
#define CD_LABEL_WHITE      ((CdLabel)255u)
#else
typedef uint16 CdLabel;
#define CD_LABEL_WHITE      ((CdLabel)65535u)
#endif
#if COLOR_DETECTION_COLOR_COUNT < 1u || COLOR_DETECTION_COLOR_COUNT > 65534u
#error COLOR_DETECTION_COLOR_COUNT must be between 1 and 65534
#endif

#define CD_IMAGE_PIXELS     ((uint32)SCC8660_W * (uint32)SCC8660_H)

/* One or two bytes per pixel according to the exported colour count. */
static CdLabel s_label_map[SCC8660_H][SCC8660_W];

/* Linear pixel indexes: no hidden x/y < 256 limit. Preserve the original
 * two-byte queue cost for ordinary camera frames; grow only when necessary. */
#if SCC8660_W * SCC8660_H <= 65535u
typedef uint16 CdPixelIndex;
#else
typedef uint32 CdPixelIndex;
#endif
static CdPixelIndex s_bfs_queue[CD_IMAGE_PIXELS];

/* Scratch arrays for robust bottom-contact extraction. */
static uint32 s_row_x_sum[SCC8660_H];
static uint16 s_row_count[SCC8660_H];

#if COLOR_DETECTION_COPY_CAMERA_FRAME
static uint16 s_frame_copy[SCC8660_H][SCC8660_W];
#endif

const ColorDetectionThreshold g_color_detection_thresholds[] =
{
    TC4_CFG_COLOR_THRESHOLDS
};
const uint8 g_color_detection_preview_rgb[][3] = { TC4_CFG_COLOR_PREVIEW_RGB };
typedef char CdThresholdCountCheck[
    sizeof(g_color_detection_thresholds)/sizeof(g_color_detection_thresholds[0])
    == COLOR_DETECTION_COLOR_COUNT ? 1 : -1];
typedef char CdPreviewCountCheck[
    sizeof(g_color_detection_preview_rgb)/sizeof(g_color_detection_preview_rgb[0])
    == COLOR_DETECTION_COLOR_COUNT ? 1 : -1];
#if TC4_CFG_COLOR_ROI_LEFT_PERCENT >= TC4_CFG_COLOR_ROI_RIGHT_PERCENT || \
    TC4_CFG_COLOR_ROI_RIGHT_PERCENT > 100u || TC4_CFG_COLOR_ROI_TOP_PERCENT > 100u || \
    TC4_CFG_WHITE_ROI_TOP_PERCENT > 100u
#error Invalid ROI percentages in TC4ImageProcessingConfig.h
#endif
#if COLOR_DETECTION_MAX_CONES < 1 || COLOR_DETECTION_MAX_CONES > 255
#error Per-colour component cap must be between 1 and 255
#endif
#if TC4_CFG_COLOR_BGR565 > 1 || TC4_CFG_COLOR_SWAP_RGB565_BYTES > 1
#error Camera byte/channel order flags must be 0 or 1
#endif
#if COLOR_DETECTION_BOTTOM_BAND_ROWS < 1 || COLOR_DETECTION_BOTTOM_BAND_ROWS > 65535 || \
    COLOR_DETECTION_BOTTOM_MIN_PIXELS < 1 || COLOR_DETECTION_BOTTOM_MIN_PIXELS > 65535
#error Invalid bottom-contact settings
#endif
#if COLOR_DETECTION_COPY_CAMERA_FRAME > 1
#error Camera copy switch must be 0 or 1
#endif

typedef struct
{
    uint16 xmin;
    uint16 xmax;
    uint16 ymin;
    uint16 ymax;
    uint32 area;
} CdBlobStats;

static uint16 cd_swap_u16(uint16 value)
{
    return (uint16)((value << 8) | (value >> 8));
}

static uint8 cd_max3(uint8 a, uint8 b, uint8 c)
{
    uint8 m = (a > b) ? a : b;
    return (m > c) ? m : c;
}

static uint8 cd_min3(uint8 a, uint8 b, uint8 c)
{
    uint8 m = (a < b) ? a : b;
    return (m < c) ? m : c;
}

/* Exact bit replication from RGB565 to 8-bit RGB. */
static void cd_rgb565_to_rgb888(
    uint16 raw,
    uint8 *r,
    uint8 *g,
    uint8 *b)
{
#if COLOR_DETECTION_SWAP_RGB565_BYTES
    raw = cd_swap_u16(raw);
#endif

    {
        const uint8 r5 = (uint8)((raw >> 11) & 0x1Fu);
        const uint8 g6 = (uint8)((raw >> 5)  & 0x3Fu);
        const uint8 b5 = (uint8)( raw        & 0x1Fu);

        *r = (uint8)((r5 << 3) | (r5 >> 2));
        *g = (uint8)((g6 << 2) | (g6 >> 4));
        *b = (uint8)((b5 << 3) | (b5 >> 2));
#if TC4_CFG_COLOR_BGR565
        { const uint8 saved_r = *r; *r = *b; *b = saved_r; }
#endif
    }
}

/* HSL scale compatible with the original code: H,S,L in 0..240. */
static void cd_rgb_to_hsl240(
    uint8 r8,
    uint8 g8,
    uint8 b8,
    uint8 *h_out,
    uint8 *s_out,
    uint8 *l_out)
{
    const int r = (int)r8;
    const int g = (int)g8;
    const int b = (int)b8;
    const int maxv = (int)cd_max3(r8, g8, b8);
    const int minv = (int)cd_min3(r8, g8, b8);
    const int diff = maxv - minv;

    int h = 0;
    int s = 0;
    int l = (maxv + minv) * 120 / 255;

    if (diff != 0)
    {
        if (maxv == r)
        {
            h = 40 * (g - b) / diff;
            if (h < 0)
            {
                h += 240;
            }
        }
        else if (maxv == g)
        {
            h = 80 + 40 * (b - r) / diff;
        }
        else
        {
            h = 160 + 40 * (r - g) / diff;
        }

        if (l <= 120)
        {
            const int denom = maxv + minv;
            s = (denom != 0) ? (diff * 240 / denom) : 0;
        }
        else
        {
            const int denom = 510 - (maxv + minv);
            s = (denom != 0) ? (diff * 240 / denom) : 240;
        }
    }

    if (h < 0)   h = 0;
    if (h > 240) h = 240;
    if (s < 0)   s = 0;
    if (s > 240) s = 240;
    if (l < 0)   l = 0;
    if (l > 240) l = 240;

    *h_out = (uint8)h;
    *s_out = (uint8)s;
    *l_out = (uint8)l;
}

static uint8 cd_hsl_match(
    uint8 h,
    uint8 s,
    uint8 l,
    const ColorDetectionThreshold *t)
{
    uint8 h_ok;

    if (t->h_max >= t->h_min)
    {
        h_ok = (uint8)((h >= t->h_min) && (h <= t->h_max));
    }
    else
    {
        /* Circular Hue interval, e.g. red 198..240 OR 0..22. */
        h_ok = (uint8)((h >= t->h_min) || (h <= t->h_max));
    }

    return (uint8)(
        h_ok &&
        (s >= t->s_min) && (s <= t->s_max) &&
        (l >= t->l_min) && (l <= t->l_max));
}

static uint8 cd_is_white_sign_candidate(uint8 r, uint8 g, uint8 b)
{
    const uint8 maxc = cd_max3(r, g, b);
    const uint8 minc = cd_min3(r, g, b);

    return (uint8)(
        (minc >= COLOR_DETECTION_WHITE_MIN_CHANNEL) &&
        ((uint8)(maxc - minc) <= COLOR_DETECTION_WHITE_MAX_CHROMA));
}

static CdLabel cd_classify_pixel(uint16 raw_rgb565, ColorDetectionResult *result)
{
    uint8 r;
    uint8 g;
    uint8 b;
    uint8 h;
    uint8 s;
    uint8 l;
    uint16 colour_index;

    cd_rgb565_to_rgb888(raw_rgb565, &r, &g, &b);

    cd_rgb_to_hsl240(r, g, b, &h, &s, &l);
    result->hsl_conversions++;
    for (colour_index = 0u; colour_index < COLOR_DETECTION_COLOR_COUNT; ++colour_index)
    {
        if (cd_hsl_match(h, s, l, &g_color_detection_thresholds[colour_index]))
        {
            return (CdLabel)(colour_index + 1u);
        }
    }

    return CD_LABEL_NONE;
}

static void cd_build_label_map(
    const uint16 image[SCC8660_H][SCC8660_W],
    ColorDetectionResult *result)
{
    uint16 y;
    uint16 x;

    /* Explicitly clear outside/previous ROI labels.
     * Deterministic and safer when ROI changes.
     */
    memset(s_label_map, 0, sizeof(s_label_map));

    for (y = COLOR_DETECTION_ROI_Y_MIN;
         y <= COLOR_DETECTION_ROI_Y_MAX;
         ++y)
    {
        for (x = COLOR_DETECTION_ROI_X_MIN;
             x < COLOR_DETECTION_ROI_X_END;
             ++x)
        {
            result->roi_pixels++;
            s_label_map[y][x] = cd_classify_pixel(image[y][x], result);
        }
    }
}

static CdPixelIndex cd_pack_xy(uint16 x, uint16 y)
{
    return (CdPixelIndex)((uint32)y * SCC8660_W + x);
}

static uint16 cd_unpack_x(CdPixelIndex packed)
{
    return (uint16)(packed % SCC8660_W);
}

static uint16 cd_unpack_y(CdPixelIndex packed)
{
    return (uint16)(packed / SCC8660_W);
}

static void cd_enqueue_if_same_label(
    int nx,
    int ny,
    CdLabel label,
    uint32 *tail)
{
    if ((nx < (int)COLOR_DETECTION_ROI_X_MIN) ||
        (nx >= (int)COLOR_DETECTION_ROI_X_END) ||
        (ny < (int)((label == CD_LABEL_WHITE) ?
                    COLOR_DETECTION_WHITE_ROI_Y_MIN : COLOR_DETECTION_ROI_Y_MIN)) ||
        (ny > (int)COLOR_DETECTION_ROI_Y_MAX))
    {
        return;
    }

    if (s_label_map[ny][nx] != label)
    {
        return;
    }

    if (*tail >= CD_IMAGE_PIXELS)
    {
        return;
    }

    /* Mark consumed BEFORE enqueue: each pixel enters queue at most once. */
    s_label_map[ny][nx] = CD_LABEL_NONE;
    s_bfs_queue[*tail] = cd_pack_xy((uint16)nx, (uint16)ny);
    (*tail)++;
}

static uint8 cd_flood_fill(
    uint16 sx,
    uint16 sy,
    CdLabel label,
    CdBlobStats *stats)
{
    uint32 head = 0u;
    uint32 tail = 0u;

    if ((stats == 0) || (s_label_map[sy][sx] != label))
    {
        return 0u;
    }

    memset(s_row_x_sum, 0, sizeof(s_row_x_sum));
    memset(s_row_count, 0, sizeof(s_row_count));

    stats->xmin = sx;
    stats->xmax = sx;
    stats->ymin = sy;
    stats->ymax = sy;
    stats->area = 0u;

    s_label_map[sy][sx] = CD_LABEL_NONE;
    s_bfs_queue[tail++] = cd_pack_xy(sx, sy);

    while (head < tail)
    {
        const CdPixelIndex packed = s_bfs_queue[head++];
        const uint16 x = cd_unpack_x(packed);
        const uint16 y = cd_unpack_y(packed);

        stats->area++;

        if (x < stats->xmin) stats->xmin = x;
        if (x > stats->xmax) stats->xmax = x;
        if (y < stats->ymin) stats->ymin = y;
        if (y > stats->ymax) stats->ymax = y;

        s_row_x_sum[y] += x;
        s_row_count[y]++;

        cd_enqueue_if_same_label((int)x - 1, (int)y,     label, &tail);
        cd_enqueue_if_same_label((int)x + 1, (int)y,     label, &tail);
        cd_enqueue_if_same_label((int)x,     (int)y - 1, label, &tail);
        cd_enqueue_if_same_label((int)x,     (int)y + 1, label, &tail);
    }

    return 1u;
}

static uint8 cd_blob_passes_geometry(
    const CdBlobStats *stats,
    const ColorDetectionThreshold *t,
    uint16 *width_out,
    uint16 *height_out,
    uint16 *fill_out,
    uint16 *aspect_out)
{
    uint32 bbox_area;
    uint32 fill;
    uint32 aspect;
    uint16 width;
    uint16 height;

    if ((stats == 0) || (t == 0))
    {
        return 0u;
    }

    if ((stats->area < t->area_min) || (stats->area > t->area_max))
    {
        return 0u;
    }

    width  = (uint16)(stats->xmax - stats->xmin + 1u);
    height = (uint16)(stats->ymax - stats->ymin + 1u);

    if ((width < t->width_min) || (height < t->height_min))
    {
        return 0u;
    }

    bbox_area = (uint32)width * (uint32)height;
    if (bbox_area == 0u)
    {
        return 0u;
    }

    fill = ((uint32)stats->area * 1000u) / bbox_area;
    aspect = ((uint32)height * 1000u) / (uint32)width;

    if (fill < t->fill_min_permille)
    {
        return 0u;
    }

    if ((aspect < t->aspect_min_permille) ||
        (aspect > t->aspect_max_permille))
    {
        return 0u;
    }

    *width_out  = width;
    *height_out = height;
    *fill_out   = (uint16)fill;
    *aspect_out = (uint16)aspect;
    return 1u;
}

/* Robust approximation of cone-ground contact:
 * 1) Find the lowest row with at least BOTTOM_MIN_PIXELS in this component.
 * 2) Use that row and a few rows above to average x.
 * 3) Keep v at the selected lowest reliable row.
 *
 * If no row reaches the minimum count, fall back to ymax.
 */
static void cd_extract_bottom_contact(
    const CdBlobStats *stats,
    uint16 *u_out,
    uint16 *v_out)
{
    int y;
    int base_y = (int)stats->ymax;
    uint32 sum_x = 0u;
    uint32 count = 0u;
    uint16 band;

    for (y = (int)stats->ymax; y >= (int)stats->ymin; --y)
    {
        if (s_row_count[y] >= COLOR_DETECTION_BOTTOM_MIN_PIXELS)
        {
            base_y = y;
            break;
        }
    }

    for (band = 0u; band < COLOR_DETECTION_BOTTOM_BAND_ROWS; ++band)
    {
        const int yy = base_y - (int)band;
        if (yy < (int)stats->ymin)
        {
            break;
        }

        sum_x += s_row_x_sum[yy];
        count += s_row_count[yy];
    }

    if (count == 0u)
    {
        *u_out = (uint16)((stats->xmin + stats->xmax) / 2u);
    }
    else
    {
        *u_out = (uint16)(sum_x / count);
    }

    *v_out = (uint16)base_y;
}

static void cd_store_blob(
    CdLabel label,
    const CdBlobStats *stats,
    uint16 width,
    uint16 height,
    uint16 fill_permille,
    uint16 aspect_permille,
    ColorDetectionResult *result)
{
    ColorDetectionCone *cone = 0;
    CameraGround2f ground;
    uint16 u;
    uint16 v;

    if ((label == CD_LABEL_NONE) || (label > COLOR_DETECTION_COLOR_COUNT))
    {
        return;
    }
    if (result->colours[label - 1u].count >= COLOR_DETECTION_MAX_CONES)
    {
        return;
    }
    cone = &result->colours[label - 1u].cones[result->colours[label - 1u].count++];
    cone->color_index = (uint16)(label - 1u);

    cd_extract_bottom_contact(stats, &u, &v);

    cone->pixel_u = u;
    cone->pixel_v = v;

    cone->xmin = stats->xmin;
    cone->xmax = stats->xmax;
    cone->ymin = stats->ymin;
    cone->ymax = stats->ymax;
    cone->width = width;
    cone->height = height;
    cone->area = stats->area;
    cone->fill_permille = fill_permille;
    cone->aspect_permille = aspect_permille;

    cone->ground_x = 0.0f;
    cone->ground_y = 0.0f;
    cone->ipm_valid = 0u;

    if (Camera_RawPixelToGround((float)u, (float)v, &ground))
    {
        cone->ground_x = ground.x;
        cone->ground_y = ground.y;
        cone->ipm_valid = 1u;
    }
}

/* Queue still contains the last component, so painting needs no extra mask. */
static void cd_paint_component(
    const CdBlobStats *stats, CdLabel label,
    uint8 preview[SCC8660_H][SCC8660_W][3])
{
    uint32 n;
    uint16 x, y;
    if (preview == 0) return;
    for (n = 0u; n < stats->area; ++n)
        memcpy(preview[cd_unpack_y(s_bfs_queue[n])][cd_unpack_x(s_bfs_queue[n])],
               g_color_detection_preview_rgb[label - 1u], 3u);
    for (x = stats->xmin; x <= stats->xmax; ++x) {
        memset(preview[stats->ymin][x], 255, 3u);
        memset(preview[stats->ymax][x], 255, 3u);
    }
    for (y = stats->ymin; y <= stats->ymax; ++y) {
        memset(preview[y][stats->xmin], 255, 3u);
        memset(preview[y][stats->xmax], 255, 3u);
    }
}

static void cd_scan_components(
    ColorDetectionResult *result, uint8 preview[SCC8660_H][SCC8660_W][3])
{
    uint16 y;
    uint16 x;

    for (y = COLOR_DETECTION_ROI_Y_MIN;
         y <= COLOR_DETECTION_ROI_Y_MAX;
         ++y)
    {
        for (x = COLOR_DETECTION_ROI_X_MIN;
             x < COLOR_DETECTION_ROI_X_END;
             ++x)
        {
            const CdLabel label = s_label_map[y][x];

            if ((label != CD_LABEL_NONE) && (label <= COLOR_DETECTION_COLOR_COUNT))
            {
                CdBlobStats stats;
                const ColorDetectionThreshold *t;
                uint16 width = 0u;
                uint16 height = 0u;
                uint16 fill = 0u;
                uint16 aspect = 0u;

                result->connected_components++;

                if (!cd_flood_fill(x, y, label, &stats))
                {
                    continue;
                }

                t = &g_color_detection_thresholds[label - 1u];

                if (!cd_blob_passes_geometry(
                        &stats, t,
                        &width, &height,
                        &fill, &aspect))
                {
                    result->rejected_components++;
                    continue;
                }

                if (result->colours[label - 1u].count >= COLOR_DETECTION_MAX_CONES)
                    continue;
                cd_paint_component(&stats, label, preview);
                cd_store_blob(
                    label,
                    &stats,
                    width,
                    height,
                    fill,
                    aspect,
                    result);
            }
        }
    }
}

static void cd_update_largest_white_region(
    const CdBlobStats *stats,
    ColorDetectionResult *result)
{
    CameraGround2f ground;
    uint16 u;
    uint16 v;

    if ((stats == 0) || (result == 0) ||
        (stats->area <= result->largest_white_component_area))
    {
        return;
    }

    result->largest_white_component_area = stats->area;
    result->large_white_detected =
        (uint8)(stats->area >= COLOR_DETECTION_WHITE_MIN_AREA);
    result->white_sign_pixel_u = 0u;
    result->white_sign_pixel_v = 0u;
    result->white_sign_ground_x = 0.0f;
    result->white_sign_ground_y = 0.0f;
    result->white_sign_angle_error_rad = 0.0f;
    result->white_sign_ipm_valid = 0u;

    if (!result->large_white_detected)
    {
        return;
    }

    /* The homography is valid on the ground plane, therefore project the
     * board's lowest visible contact point rather than its image centroid. */
    cd_extract_bottom_contact(stats, &u, &v);
    result->white_sign_pixel_u = u;
    result->white_sign_pixel_v = v;

    if (Camera_RawPixelToGround((float)u, (float)v, &ground))
    {
        result->white_sign_ground_x = ground.x;
        result->white_sign_ground_y = ground.y;
        result->white_sign_angle_error_rad = atan2f(ground.y, ground.x);
        result->white_sign_ipm_valid = 1u;
    }
}

/* The cone flood fills have consumed their labels by this point, so reuse the
 * existing map and BFS queue instead of allocating another full-frame mask. */
static void cd_detect_large_white_region(
    const uint16 image[SCC8660_H][SCC8660_W],
    ColorDetectionResult *result)
{
    uint16 y;
    uint16 x;

    memset(s_label_map, 0, sizeof(s_label_map));

    for (y = COLOR_DETECTION_WHITE_ROI_Y_MIN;
         y <= COLOR_DETECTION_WHITE_ROI_Y_MAX;
         ++y)
    {
        for (x = COLOR_DETECTION_ROI_X_MIN;
             x < COLOR_DETECTION_ROI_X_END;
             ++x)
        {
            uint8 r;
            uint8 g;
            uint8 b;

            cd_rgb565_to_rgb888(image[y][x], &r, &g, &b);
            if (cd_is_white_sign_candidate(r, g, b))
            {
                s_label_map[y][x] = CD_LABEL_WHITE;
                result->white_candidate_pixels++;
            }
        }
    }

    for (y = COLOR_DETECTION_WHITE_ROI_Y_MIN;
         y <= COLOR_DETECTION_WHITE_ROI_Y_MAX;
         ++y)
    {
        for (x = COLOR_DETECTION_ROI_X_MIN;
             x < COLOR_DETECTION_ROI_X_END;
             ++x)
        {
            if (s_label_map[y][x] == CD_LABEL_WHITE)
            {
                CdBlobStats stats;

                result->white_connected_components++;
                if (!cd_flood_fill(x, y, CD_LABEL_WHITE, &stats))
                {
                    continue;
                }

                cd_update_largest_white_region(&stats, result);
            }
        }
    }
}

void ColorDetection_Init(void)
{
    memset(s_label_map, 0, sizeof(s_label_map));
#if COLOR_DETECTION_COPY_CAMERA_FRAME
    memset(s_frame_copy, 0, sizeof(s_frame_copy));
#endif
}

uint32 ColorDetection_ProcessImage(
    const uint16 image[SCC8660_H][SCC8660_W],
    ColorDetectionResult *result)
{
    return ColorDetection_ProcessImageWithPreview(image, result, 0);
}

const ColorDetectionThreshold *ColorDetection_GetThreshold(uint16 color_index)
{
    return color_index < COLOR_DETECTION_COLOR_COUNT ?
           &g_color_detection_thresholds[color_index] : 0;
}

uint8 ColorDetection_Rgb565ToHsl(uint16 raw, uint8 *h, uint8 *s, uint8 *l)
{
    uint8 r, g, b;
    if (h == 0 || s == 0 || l == 0) return 0u;
    cd_rgb565_to_rgb888(raw, &r, &g, &b);
    cd_rgb_to_hsl240(r, g, b, h, s, l);
    return 1u;
}

uint32 ColorDetection_ProcessImageWithPreview(
    const uint16 image[SCC8660_H][SCC8660_W],
    ColorDetectionResult *result,
    uint8 preview[SCC8660_H][SCC8660_W][3])
{
    if ((image == 0) || (result == 0))
    {
        return 0u;
    }

    memset(result, 0, sizeof(*result));
    if (preview != 0)
        memset(preview, 0, (size_t)SCC8660_H * SCC8660_W * 3u);

    cd_build_label_map(image, result);
    cd_scan_components(result, preview);
    cd_detect_large_white_region(image, result);

    {
        uint16 colour_index;
        uint32 total = 0u;
        for (colour_index = 0u; colour_index < COLOR_DETECTION_COLOR_COUNT; ++colour_index)
        {
            total += result->colours[colour_index].count;
        }
        return total;
    }
}

uint8 ColorDetection_ProcessCameraFrame(ColorDetectionResult *result)
{
    if (result == 0)
    {
        return 0u;
    }

    if (!scc8660_finish_flag)
    {
        return 0u;
    }

#if COLOR_DETECTION_COPY_CAMERA_FRAME
    memcpy(s_frame_copy, scc8660_image, sizeof(s_frame_copy));
    scc8660_finish_flag = 0u;
    (void)ColorDetection_ProcessImage(s_frame_copy, result);
#else
    scc8660_finish_flag = 0u;
    (void)ColorDetection_ProcessImage(scc8660_image, result);
#endif

    return 1u;
}

uint8 ColorDetection_SetWhiteBalance(uint16 wb_value)
{
    /* Driver documentation:
     * 0          -> automatic white balance
     * 0x65..0xA0 -> manual white balance
     */
    if ((wb_value != 0u) &&
        ((wb_value < 0x65u) || (wb_value > 0xA0u)))
    {
        return 1u;
    }

    return scc8660_set_white_balance(wb_value);
}
