/*
 * ColorDetection.c
 *
 * One-pass red/yellow color classification + connected components + CameraIPM.
 */

#include "Header.h"

#define CD_LABEL_NONE       ((uint8)COLOR_DETECTION_NONE)
#define CD_LABEL_RED        ((uint8)COLOR_DETECTION_RED)
#define CD_LABEL_YELLOW     ((uint8)COLOR_DETECTION_YELLOW)
#define CD_LABEL_WHITE      ((uint8)3u)

#define CD_IMAGE_PIXELS     ((uint32)SCC8660_W * (uint32)SCC8660_H)

/* One byte per pixel. The map is consumed by flood fill, so no visit_mask is needed. */
static uint8 s_label_map[SCC8660_H][SCC8660_W];

/* x < 256 and y < 256, therefore one uint16 can pack both coordinates:
 * high byte = y, low byte = x.
 */
static uint16 s_bfs_queue[CD_IMAGE_PIXELS];

/* Scratch arrays for robust bottom-contact extraction. */
static uint32 s_row_x_sum[SCC8660_H];
static uint16 s_row_count[SCC8660_H];

#if COLOR_DETECTION_COPY_CAMERA_FRAME
static uint16 s_frame_copy[SCC8660_H][SCC8660_W];
#endif

/* Conservative thresholds for the 1.5 m guidance window. Ground stains are
 * typically lower in saturation/brightness, sparse inside their bounding
 * box, or wider than tall; all of those properties are rejected below. */
ColorDetectionThreshold g_color_detection_red_threshold =
{
		230u, 15u,        // H：跨越0点，230~240 或 0~15
		20u,  240u,       // S
		65u,  240u,       // L：原80降低到65

		10u,  12000u,
		2u,   3u,
		70u,
		300u, 5000u
};

ColorDetectionThreshold g_color_detection_yellow_threshold =
{
		18u,  42u,        // H：原24~40放宽
		80u,  240u,       // S：原120降低到80
		70u,  240u,       // L：原120降低到70

		10u,  12000u,
		2u,   3u,
		70u,
		300u, 5000u
};

typedef struct
{
    uint16 xmin;
    uint16 xmax;
    uint16 ymin;
    uint16 ymax;
    uint16 area;
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

/* Extremely cheap reject for obvious blue/neutral background.
 *
 * Red needs R to dominate at least B.
 * Yellow needs both R and G to dominate B.
 * If B is >= both R and G, the pixel cannot be a useful red/yellow candidate
 * for this detector, so skip the HSL divisions.
 */
static uint8 cd_fast_red_yellow_candidate(uint8 r, uint8 g, uint8 b)
{
    if ((b >= r) && (b >= g))
    {
        return 0u;
    }
    return 1u;
}

static uint8 cd_is_white_sign_candidate(uint8 r, uint8 g, uint8 b)
{
    const uint8 maxc = cd_max3(r, g, b);
    const uint8 minc = cd_min3(r, g, b);

    return (uint8)(
        (minc >= COLOR_DETECTION_WHITE_MIN_CHANNEL) &&
        ((uint8)(maxc - minc) <= COLOR_DETECTION_WHITE_MAX_CHROMA));
}

static uint8 cd_classify_pixel(uint16 raw_rgb565, ColorDetectionResult *result)
{
    uint8 r;
    uint8 g;
    uint8 b;
    uint8 h;
    uint8 s;
    uint8 l;

    cd_rgb565_to_rgb888(raw_rgb565, &r, &g, &b);

    if (!cd_fast_red_yellow_candidate(r, g, b))
    {
        result->fast_rejected_pixels++;
        return CD_LABEL_NONE;
    }

    cd_rgb_to_hsl240(r, g, b, &h, &s, &l);
    result->hsl_conversions++;

    if (cd_hsl_match(h, s, l, &g_color_detection_red_threshold))
    {
        return CD_LABEL_RED;
    }

    if (cd_hsl_match(h, s, l, &g_color_detection_yellow_threshold))
    {
        return CD_LABEL_YELLOW;
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
     * 20 KB at 160x128; deterministic and safer when ROI changes.
     */
    memset(s_label_map, 0, sizeof(s_label_map));

    for (y = COLOR_DETECTION_ROI_Y_MIN;
         y <= COLOR_DETECTION_ROI_Y_MAX;
         ++y)
    {
        for (x = COLOR_DETECTION_ROI_X_MIN;
             x <= COLOR_DETECTION_ROI_X_MAX;
             ++x)
        {
            result->roi_pixels++;
            s_label_map[y][x] = cd_classify_pixel(image[y][x], result);
        }
    }
}

static uint16 cd_pack_xy(uint16 x, uint16 y)
{
    return (uint16)(((y & 0xFFu) << 8) | (x & 0xFFu));
}

static uint16 cd_unpack_x(uint16 packed)
{
    return (uint16)(packed & 0xFFu);
}

static uint16 cd_unpack_y(uint16 packed)
{
    return (uint16)((packed >> 8) & 0xFFu);
}

static void cd_enqueue_if_same_label(
    int nx,
    int ny,
    uint8 label,
    uint32 *tail)
{
    if ((nx < (int)COLOR_DETECTION_ROI_X_MIN) ||
        (nx > (int)COLOR_DETECTION_ROI_X_MAX) ||
        (ny < (int)COLOR_DETECTION_ROI_Y_MIN) ||
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
    uint8 label,
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
        const uint16 packed = s_bfs_queue[head++];
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
    uint8 band;

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
    uint8 label,
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

    if (label == CD_LABEL_RED)
    {
        if (result->red_count >= COLOR_DETECTION_MAX_CONES)
        {
            return;
        }
        cone = &result->red[result->red_count++];
        cone->color = COLOR_DETECTION_RED;
    }
    else if (label == CD_LABEL_YELLOW)
    {
        if (result->yellow_count >= COLOR_DETECTION_MAX_CONES)
        {
            return;
        }
        cone = &result->yellow[result->yellow_count++];
        cone->color = COLOR_DETECTION_YELLOW;
    }
    else
    {
        return;
    }

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

static void cd_scan_components(ColorDetectionResult *result)
{
    uint16 y;
    uint16 x;

    for (y = COLOR_DETECTION_ROI_Y_MIN;
         y <= COLOR_DETECTION_ROI_Y_MAX;
         ++y)
    {
        for (x = COLOR_DETECTION_ROI_X_MIN;
             x <= COLOR_DETECTION_ROI_X_MAX;
             ++x)
        {
            const uint8 label = s_label_map[y][x];

            if ((label == CD_LABEL_RED) || (label == CD_LABEL_YELLOW))
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

                t = (label == CD_LABEL_RED) ?
                    &g_color_detection_red_threshold :
                    &g_color_detection_yellow_threshold;

                if (!cd_blob_passes_geometry(
                        &stats, t,
                        &width, &height,
                        &fill, &aspect))
                {
                    result->rejected_components++;
                    continue;
                }

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
             x <= COLOR_DETECTION_ROI_X_MAX;
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
             x <= COLOR_DETECTION_ROI_X_MAX;
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

uint8 ColorDetection_ProcessImage(
    const uint16 image[SCC8660_H][SCC8660_W],
    ColorDetectionResult *result)
{
    if ((image == 0) || (result == 0))
    {
        return 0u;
    }

    memset(result, 0, sizeof(*result));

    cd_build_label_map(image, result);
    cd_scan_components(result);
    cd_detect_large_white_region(image, result);

    return (uint8)(result->red_count + result->yellow_count);
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
