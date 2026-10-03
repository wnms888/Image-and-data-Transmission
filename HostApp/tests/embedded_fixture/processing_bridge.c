/* Exposes production C implementations to Python without struct ABI assumptions.
 * Hardware stubs are intentionally test-only. No algorithms are reimplemented. */
#include "Header.h"
#include "ColorDetection.h"
#include "CameraIPM.h"
#include "WifiImageTransfer.h"
#ifdef _WIN32
#define TEST_API __declspec(dllexport)
#else
#define TEST_API
#endif
uint16 scc8660_image[SCC8660_H][SCC8660_W];
uint8 scc8660_finish_flag;
static ColorDetectionResult result;
static uint8 wire[SCC8660_IMAGE_SIZE+65536u];
static uint32 wire_length;
uint8 scc8660_set_white_balance(uint16 value) { (void)value; return 0u; }
void ips200_show_string(int x,int y,const char *text) { (void)x; (void)y; (void)text; }
void system_delay_ms(int ms) { (void)ms; }
uint8 wifi_spi_init(const char *s,const char *p) { (void)s; (void)p; return 0u; }
uint8 wifi_spi_wifi_connect(const char *s,const char *p) { (void)s; (void)p; return 0u; }
uint8 wifi_spi_socket_connect(const char *k,const char *i,const char *p,const char *l)
{ (void)k; (void)i; (void)p; (void)l; return 0u; }
uint32 wifi_spi_stream_send_buffer(const uint8 *data,uint32 length)
{
    /* Small fragments also exercise production header/payload fragmentation. */
    uint32 used=length>37u ? 37u : length;
    if (wire_length+used>sizeof(wire)) return 0u;
    memcpy(wire+wire_length,data,used); wire_length+=used; return used;
}
TEST_API uint32 Test_Color(const uint8 *raw,uint8 *preview)
{
    memcpy(scc8660_image,raw,SCC8660_IMAGE_SIZE);
    return ColorDetection_ProcessImageWithPreview(scc8660_image,&result,
                                                 (uint8 (*)[SCC8660_W][3])preview);
}
TEST_API uint32 Test_Count(uint32 index) { return result.colours[index].count; }
TEST_API uint32 Test_WhiteArea(void) { return result.largest_white_component_area; }
TEST_API uint32 Test_WhiteComponents(void) { return result.white_connected_components; }
TEST_API uint32 Test_RoiPixels(void) { return result.roi_pixels; }
TEST_API uint32 Test_Ipm(const uint8 *raw,uint8 *output)
{
    memcpy(scc8660_image,raw,SCC8660_IMAGE_SIZE);
    return Camera_RenderIpmRgb565(scc8660_image,
                                 (uint16 (*)[CAMERA_IPM_OUTPUT_WIDTH])output);
}
TEST_API void Test_AllHsl(uint8 *output)
{
    uint32 value;
    for (value=0u;value<65536u;++value)
        ColorDetection_Rgb565ToHsl((uint16)value,output+value*3u,
                                  output+value*3u+1u,output+value*3u+2u);
}
TEST_API uint32 Test_ImagePacket(const uint8 *raw,uint8 *output)
{
    WifiImageTransfer_Init(); wire_length=0u;
    memcpy(scc8660_image,raw,SCC8660_IMAGE_SIZE);
    WifiImageTransfer_SubmitRgb565Frame(&scc8660_image[0][0],SCC8660_IMAGE_SIZE);
    memcpy(output,wire,wire_length); return wire_length;
}
TEST_API uint32 Test_TextPacket(const char *text,uint32 length,uint8 *output)
{
    uint32 n;
    wire_length=0u;
    WifiImageTransfer_WriteLogBytes(text,length);
    for(n=0u;n<100u;++n) WifiImageTransfer_Service();
    memcpy(output,wire,wire_length); return wire_length;
}
