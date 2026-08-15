#ifndef CODE_WIFI_IMAGE_TRANSFER_H_
#define CODE_WIFI_IMAGE_TRANSFER_H_

#include "zf_common_headfile.h"

/* Select exactly one image-transfer behaviour at build time. */
#define WIFI_IMAGE_TRANSFER_DISABLED             (0u)
#define WIFI_IMAGE_TRANSFER_AFTER_PROCESSING     (1u)

#ifndef WIFI_IMAGE_TRANSFER_MODE
#define WIFI_IMAGE_TRANSFER_MODE WIFI_IMAGE_TRANSFER_AFTER_PROCESSING
#endif

/* PC mobile-hotspot and SeekFree Assistant endpoint. */
#ifndef WIFI_IMAGE_TRANSFER_SSID
#define WIFI_IMAGE_TRANSFER_SSID                 "BRDJC"
#endif

#ifndef WIFI_IMAGE_TRANSFER_PASSWORD
#define WIFI_IMAGE_TRANSFER_PASSWORD             "12345678"
#endif

#ifndef WIFI_IMAGE_TRANSFER_TARGET_IP
#define WIFI_IMAGE_TRANSFER_TARGET_IP            "192.168.137.1"
#endif

#ifndef WIFI_IMAGE_TRANSFER_TARGET_PORT
#define WIFI_IMAGE_TRANSFER_TARGET_PORT          "8086"
#endif

#ifndef WIFI_IMAGE_TRANSFER_MODULE_PROBE_ATTEMPTS
#define WIFI_IMAGE_TRANSFER_MODULE_PROBE_ATTEMPTS (3u)
#endif

/*
 * WiFi stream protocol
 * --------------------
 * Every transmission is one self-describing packet.  This deliberately
 * replaces the old image-only 0xAA header, so a printf byte can never be
 * mistaken for an RGB565 pixel (or vice versa).
 *
 * Header, little endian, 22 bytes:
 *   0..1   sync 0xAA, 0x55
 *   2      protocol version (1)
 *   3      packet type: 1 = RGB565 image, 2 = UTF-8/ASCII text
 *   4..7   payload length
 *   8..9   image width (zero for text)
 *   10..11 image height (zero for text)
 *   12     pixel format (2 = RGB565, zero for text)
 *   13     flags: bit 0 = RGB565 payload is high-byte first
 *   14..15 packet sequence
 *   16..17 CRC-16/CCITT of bytes 0..15
 *   18..21 payload CRC-32 (zero when disabled)
 */
#define WIFI_IMAGE_TRANSFER_PROTOCOL_VERSION       (1u)
#define WIFI_IMAGE_TRANSFER_PACKET_IMAGE_RGB565    (1u)
#define WIFI_IMAGE_TRANSFER_PACKET_TEXT            (2u)
#define WIFI_IMAGE_TRANSFER_PACKET_HEADER_SIZE     (22u)
#define WIFI_IMAGE_TRANSFER_RGB565_FORMAT          (2u)
#define WIFI_IMAGE_TRANSFER_FLAG_RGB565_MSB_FIRST  (0x01u)

/* SCC8660 emits the RGB565 high byte first on its pixel bus.  This switch
 * describes the byte sequence already present in image memory; it does not
 * alter or copy the image payload.  Set to 0 only when the supplied image
 * buffer is known to be native little-endian RGB565. */
#ifndef WIFI_IMAGE_TRANSFER_RGB565_MSB_FIRST
#define WIFI_IMAGE_TRANSFER_RGB565_MSB_FIRST       (1u)
#endif

/* Maximum one printf line staged before its terminating '\n'. */
#ifndef WIFI_IMAGE_TRANSFER_TEXT_MAX_LENGTH
#define WIFI_IMAGE_TRANSFER_TEXT_MAX_LENGTH        (256u)
#endif

/* Disable by default to retain maximum image-frame throughput.  The PC
 * receiver accepts a zero payload CRC as "not supplied". */
#ifndef WIFI_IMAGE_TRANSFER_ENABLE_PAYLOAD_CRC
#define WIFI_IMAGE_TRANSFER_ENABLE_PAYLOAD_CRC     (0u)
#endif

#if (WIFI_IMAGE_TRANSFER_MODE != WIFI_IMAGE_TRANSFER_DISABLED) && \
    (WIFI_IMAGE_TRANSFER_MODE != WIFI_IMAGE_TRANSFER_AFTER_PROCESSING)
#error "WIFI_IMAGE_TRANSFER_MODE must be DISABLED or AFTER_PROCESSING."
#endif

#if (WIFI_IMAGE_TRANSFER_MODULE_PROBE_ATTEMPTS == 0u)
#error "WIFI_IMAGE_TRANSFER_MODULE_PROBE_ATTEMPTS must be greater than zero."
#endif

#if (WIFI_IMAGE_TRANSFER_TEXT_MAX_LENGTH < 2u)
#error "WIFI_IMAGE_TRANSFER_TEXT_MAX_LENGTH must be at least 2."
#endif

#if (WIFI_IMAGE_TRANSFER_TEXT_MAX_LENGTH > 65535u)
#error "WIFI_IMAGE_TRANSFER_TEXT_MAX_LENGTH must fit in uint16."
#endif

#if (WIFI_IMAGE_TRANSFER_RGB565_MSB_FIRST != 0u) && \
    (WIFI_IMAGE_TRANSFER_RGB565_MSB_FIRST != 1u)
#error "WIFI_IMAGE_TRANSFER_RGB565_MSB_FIRST must be 0 or 1."
#endif

/* Call after IPS/camera setup.  In DISABLED mode this is a no-op. */
void WifiImageTransfer_Init(void);

/*
 * Send one completed RGB565 frame after vision, PPU preparation, display and
 * telemetry work.  This is a synchronous, maximum-throughput call: the source
 * image must remain valid until it returns, and CPU0 waits until every SPI
 * fragment has been submitted to the WiFi module.
 */
void WifiImageTransfer_SubmitRgb565Frame(const uint16 *image, uint32 image_bytes);

/*
 * Send an already assembled debug line as a type-2 packet.  Text is normally
 * sent through WifiImageTransfer_PutChar(), which is designed to be called by
 * the project's printf redirection function.  The payload should end in '\n'.
 */
uint8 WifiImageTransfer_SubmitText(const char *text, uint16 text_length);

/*
 * printf redirection entry points.  Add the indicated one-line call to the
 * existing fputc/_write hook (see HostApp/README.zh-CN.md):
 *
 *     WifiImageTransfer_PutChar((char)ch);
 *
 * A CR is ignored and a LF closes and sends one text packet.  Thus ordinary
 * calls such as printf("[info] Base_StartWaitingIMU\\n"); are transported
 * without changing application code.
 */
void WifiImageTransfer_PutChar(char character);
void WifiImageTransfer_WriteLogBytes(const char *data, uint32 length);
void WifiImageTransfer_FlushLog(void);

uint8 WifiImageTransfer_IsReady(void);

#endif /* CODE_WIFI_IMAGE_TRANSFER_H_ */
