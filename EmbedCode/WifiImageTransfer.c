#include "WifiImageTransfer.h"
#include "zf_device_wifi_spi.h"

#if (WIFI_IMAGE_TRANSFER_MODE == WIFI_IMAGE_TRANSFER_AFTER_PROCESSING)

static uint8 s_wifi_ready = 0u;
static uint8 s_wifi_tx_active = 0u;
static uint16 s_wifi_sequence = 0u;
static char s_wifi_text_buffer[WIFI_IMAGE_TRANSFER_TEXT_MAX_LENGTH];
static uint16 s_wifi_text_length = 0u;

/* CRC-16/CCITT-FALSE: initial value 0xFFFF, polynomial 0x1021. */
static uint16 wifi_image_transfer_crc16(const uint8 *data, uint32 length)
{
    uint16 crc = 0xFFFFu;
    uint8 bit;

    while(length-- != 0u)
    {
        crc ^= (uint16)(*data++) << 8;
        for(bit = 0u; bit < 8u; ++bit)
        {
            crc = (crc & 0x8000u) ? (uint16)((crc << 1) ^ 0x1021u)
                                  : (uint16)(crc << 1);
        }
    }
    return crc;
}

#if (WIFI_IMAGE_TRANSFER_ENABLE_PAYLOAD_CRC != 0u)
static uint32 wifi_image_transfer_crc32(const uint8 *data, uint32 length)
{
    uint32 crc = 0xFFFFFFFFu;
    uint8 bit;

    while(length-- != 0u)
    {
        crc ^= *data++;
        for(bit = 0u; bit < 8u; ++bit)
        {
            crc = (crc & 1u) ? ((crc >> 1) ^ 0xEDB88320u) : (crc >> 1);
        }
    }
    return crc ^ 0xFFFFFFFFu;
}
#endif

static void wifi_image_transfer_put_u16_le(uint8 *destination, uint16 value)
{
    destination[0] = (uint8)(value & 0xFFu);
    destination[1] = (uint8)(value >> 8);
}

static void wifi_image_transfer_put_u32_le(uint8 *destination, uint32 value)
{
    destination[0] = (uint8)(value & 0xFFu);
    destination[1] = (uint8)((value >> 8) & 0xFFu);
    destination[2] = (uint8)((value >> 16) & 0xFFu);
    destination[3] = (uint8)((value >> 24) & 0xFFu);
}

/*
 * Send an entire logical buffer without yielding to the main loop.  The SPI
 * driver accepts at most WIFI_SPI_TRANSFER_SIZE bytes per transaction, so it
 * returns the consumed byte count for each fragment.  A zero result means the
 * module INT line is busy; intentionally retry immediately here.  This is the
 * highest-throughput mode: it deliberately trades CPU0 responsiveness for
 * continuous image transfer.
 */
static void wifi_image_transfer_send_all(const uint8 *buffer, uint32 length)
{
    uint32 sent_length;

    while(length != 0u)
    {
        sent_length = wifi_spi_stream_send_buffer(buffer, length);
        if(sent_length != 0u)
        {
            buffer += sent_length;
            length -= sent_length;
        }
    }
}

/* Build and emit one complete logical packet.  Keeping header and payload in
 * the same critical send section prevents interleaving between image data and
 * text emitted through the printf redirection hook. */
static uint8 wifi_image_transfer_send_packet(uint8 packet_type,
                                              const uint8 *payload,
                                              uint32 payload_length,
                                              uint16 width,
                                              uint16 height,
                                              uint8 pixel_format,
                                              uint8 flags)
{
    uint8 header[WIFI_IMAGE_TRANSFER_PACKET_HEADER_SIZE] = {0u};
    uint16 header_crc;

    if((s_wifi_ready == 0u) || ((payload == NULL) && (payload_length != 0u)) ||
       (s_wifi_tx_active != 0u))
    {
        return 0u;
    }

    s_wifi_tx_active = 1u;
    header[0] = 0xAAu;
    header[1] = 0x55u;
    header[2] = WIFI_IMAGE_TRANSFER_PROTOCOL_VERSION;
    header[3] = packet_type;
    wifi_image_transfer_put_u32_le(&header[4], payload_length);
    wifi_image_transfer_put_u16_le(&header[8], width);
    wifi_image_transfer_put_u16_le(&header[10], height);
    header[12] = pixel_format;
    header[13] = flags;
    wifi_image_transfer_put_u16_le(&header[14], s_wifi_sequence++);
    header_crc = wifi_image_transfer_crc16(header, 16u);
    wifi_image_transfer_put_u16_le(&header[16], header_crc);

#if (WIFI_IMAGE_TRANSFER_ENABLE_PAYLOAD_CRC != 0u)
    wifi_image_transfer_put_u32_le(&header[18],
                                   wifi_image_transfer_crc32(payload,
                                                             payload_length));
#endif

    wifi_image_transfer_send_all(header, (uint32)sizeof(header));
    if(payload_length != 0u)
    {
        wifi_image_transfer_send_all(payload, payload_length);
    }
    s_wifi_tx_active = 0u;
    return 1u;
}

static uint8 wifi_image_transfer_connect(void)
{
    uint8 status = 1u;
    uint8 attempt;

    ips200_show_string(0, 32, "wifi init.");
    for(attempt = 0u; attempt < WIFI_IMAGE_TRANSFER_MODULE_PROBE_ATTEMPTS; ++attempt)
    {
        status = wifi_spi_init(NULL, NULL);
        if(status == 0u)
        {
            break;
        }

        if((attempt + 1u) < WIFI_IMAGE_TRANSFER_MODULE_PROBE_ATTEMPTS)
        {
            system_delay_ms(200);
        }
    }

    if(status != 0u)
    {
        ips200_show_string(0, 48, "wifi module fail.");
        printf("\r\n wifi module probe failed (SPI3/RST/INT). \r\n");
        return 1u;
    }

    status = wifi_spi_wifi_connect(
        WIFI_IMAGE_TRANSFER_SSID, WIFI_IMAGE_TRANSFER_PASSWORD);
    if(status != 0u)
    {
        ips200_show_string(0, 48, "wifi AP fail.");
        printf("\r\n wifi AP association failed. \r\n");
        return 1u;
    }

#if (WIFI_SPI_AUTO_CONNECT == 0)
    status = wifi_spi_socket_connect(
        "TCP",
        WIFI_IMAGE_TRANSFER_TARGET_IP,
        WIFI_IMAGE_TRANSFER_TARGET_PORT,
        WIFI_SPI_LOCAL_PORT);
    if(status != 0u)
    {
        ips200_show_string(0, 48, "wifi TCP fail.");
        printf("\r\n wifi TCP connect failed: %s:%s. \r\n",
               WIFI_IMAGE_TRANSFER_TARGET_IP,
               WIFI_IMAGE_TRANSFER_TARGET_PORT);
        return 1u;
    }
#endif

    return 0u;
}

void WifiImageTransfer_Init(void)
{
    s_wifi_ready = (uint8)(wifi_image_transfer_connect() == 0u);
    if(s_wifi_ready != 0u)
    {
        ips200_show_string(0, 48, "wifi ready.");
    }
}

void WifiImageTransfer_SubmitRgb565Frame(const uint16 *image, uint32 image_bytes)
{
    if((s_wifi_ready == 0u) || (NULL == image) ||
       (image_bytes != SCC8660_IMAGE_SIZE))
    {
        return;
    }

    /* The caller sends after all processing, so image_copy remains stable for
     * this synchronous header-plus-payload transfer. */
    (void)wifi_image_transfer_send_packet(WIFI_IMAGE_TRANSFER_PACKET_IMAGE_RGB565,
                                          (const uint8 *)image,
                                          image_bytes,
                                          SCC8660_W,
                                          SCC8660_H,
                                          WIFI_IMAGE_TRANSFER_RGB565_FORMAT,
                                          (WIFI_IMAGE_TRANSFER_RGB565_MSB_FIRST != 0u) ?
                                          WIFI_IMAGE_TRANSFER_FLAG_RGB565_MSB_FIRST : 0u);
}

uint8 WifiImageTransfer_SubmitText(const char *text, uint16 text_length)
{
    if((text == NULL) || (text_length == 0u))
    {
        return 0u;
    }

    return wifi_image_transfer_send_packet(WIFI_IMAGE_TRANSFER_PACKET_TEXT,
                                           (const uint8 *)text,
                                           (uint32)text_length,
                                           0u,
                                           0u,
                                           0u,
                                           0u);
}

void WifiImageTransfer_FlushLog(void)
{
    if(s_wifi_text_length != 0u)
    {
        /* Retain unsent text while an image packet owns the SPI stream.  This
         * prevents byte interleaving without silently corrupting the log. */
        if(WifiImageTransfer_SubmitText(s_wifi_text_buffer,
                                        s_wifi_text_length) != 0u)
        {
            s_wifi_text_length = 0u;
        }
    }
}

void WifiImageTransfer_PutChar(char character)
{
    if(character == '\r')
    {
        return;
    }

    if(character == '\n')
    {
        if(s_wifi_text_length < WIFI_IMAGE_TRANSFER_TEXT_MAX_LENGTH)
        {
            s_wifi_text_buffer[s_wifi_text_length++] = character;
        }
        WifiImageTransfer_FlushLog();
        return;
    }

    if(s_wifi_text_length >= WIFI_IMAGE_TRANSFER_TEXT_MAX_LENGTH)
    {
        /* A malformed/very long print is split into valid text packets rather
         * than overwriting memory.  Normal protocol lines end in '\n'. */
        WifiImageTransfer_FlushLog();
        if(s_wifi_text_length >= WIFI_IMAGE_TRANSFER_TEXT_MAX_LENGTH)
        {
            /* The only remaining case is a concurrent SPI transmission.  Do
             * not overwrite the pending line; a later printf/FlushLog retry
             * will send it once the image packet is complete. */
            return;
        }
    }
    s_wifi_text_buffer[s_wifi_text_length++] = character;
}

void WifiImageTransfer_WriteLogBytes(const char *data, uint32 length)
{
    if(data == NULL)
    {
        return;
    }

    while(length-- != 0u)
    {
        WifiImageTransfer_PutChar(*data++);
    }
}

uint8 WifiImageTransfer_IsReady(void)
{
    return s_wifi_ready;
}

#else

void WifiImageTransfer_Init(void)
{
}

void WifiImageTransfer_SubmitRgb565Frame(const uint16 *image, uint32 image_bytes)
{
    (void)image;
    (void)image_bytes;
}

uint8 WifiImageTransfer_SubmitText(const char *text, uint16 text_length)
{
    (void)text;
    (void)text_length;
    return 0u;
}

void WifiImageTransfer_PutChar(char character)
{
    (void)character;
}

void WifiImageTransfer_WriteLogBytes(const char *data, uint32 length)
{
    (void)data;
    (void)length;
}

void WifiImageTransfer_FlushLog(void)
{
}

uint8 WifiImageTransfer_IsReady(void)
{
    return 0u;
}

#endif
