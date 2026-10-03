#ifndef CODE_WIFI_IMAGE_TRANSFER_H_
#define CODE_WIFI_IMAGE_TRANSFER_H_

#include "zf_common_headfile.h"
#include "TC4ImageProcessingConfig.h"
#if TC4_IMAGE_PROCESSING_CONFIG_VERSION != 2u
#error Export and replace the complete v2 TC4ImageProcessingConfig.h
#endif

/* Select exactly one image-transfer behaviour at build time. */
#define WIFI_IMAGE_TRANSFER_DISABLED             (0u)
#define WIFI_IMAGE_TRANSFER_AFTER_PROCESSING     (1u)

#ifndef WIFI_IMAGE_TRANSFER_MODE
#define WIFI_IMAGE_TRANSFER_MODE WIFI_IMAGE_TRANSFER_AFTER_PROCESSING
#endif

/* PC mobile-hotspot and IMGT monitor endpoint (not the old image-only tool). */
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
 *   13     flags: bit 0 = RGB565 payload is high-byte first;
 *           bit 1 = payload CRC-32 is present; bit 2 = BGR565 channel order
 *   14..15 packet sequence
 *   16..17 CRC-16/CCITT of bytes 0..15
 *   18..21 payload CRC-32/IEEE (required by the PC receiver)
 */
#define WIFI_IMAGE_TRANSFER_PROTOCOL_VERSION       (1u)
#define WIFI_IMAGE_TRANSFER_PACKET_IMAGE_RGB565    (1u)
#define WIFI_IMAGE_TRANSFER_PACKET_TEXT            (2u)
#define WIFI_IMAGE_TRANSFER_PACKET_HEADER_SIZE     (22u)
#define WIFI_IMAGE_TRANSFER_RGB565_FORMAT          (2u)
#define WIFI_IMAGE_TRANSFER_FLAG_RGB565_MSB_FIRST  (0x01u)
#define WIFI_IMAGE_TRANSFER_FLAG_PAYLOAD_CRC        (0x02u)
#define WIFI_IMAGE_TRANSFER_FLAG_BGR565             (0x04u)

/* Single source of truth: detector and WiFi describe the SAME raw memory.
 * Change only the exported configuration header, never a separate override.
 * These flags do not convert/reorder pixels or change camera driver settings. */
#if defined(WIFI_IMAGE_TRANSFER_RGB565_MSB_FIRST) && \
    WIFI_IMAGE_TRANSFER_RGB565_MSB_FIRST != TC4_CFG_COLOR_SWAP_RGB565_BYTES
#error Byte-order override conflicts with TC4ImageProcessingConfig.h
#endif
#ifndef WIFI_IMAGE_TRANSFER_RGB565_MSB_FIRST
#define WIFI_IMAGE_TRANSFER_RGB565_MSB_FIRST TC4_CFG_COLOR_SWAP_RGB565_BYTES
#endif

/* Maximum one printf line staged before its terminating '\n'. */
#ifndef WIFI_IMAGE_TRANSFER_TEXT_MAX_LENGTH
#define WIFI_IMAGE_TRANSFER_TEXT_MAX_LENGTH        (256u)
#endif

/* Ring buffer for printf text waiting to be sent.  Logging must never call
 * SPI directly because printf can run while the WiFi module is busy. */
#ifndef WIFI_IMAGE_TRANSFER_LOG_FIFO_SIZE
#define WIFI_IMAGE_TRANSFER_LOG_FIFO_SIZE          (1024u)
#endif

/*
 * The PC receiver validates every image and text payload before presenting it.
 * Keep this enabled so a corrupted frame is discarded rather than rendered or
 * parsed as debug data.  Set to 0 only for legacy/throughput experiments; the
 * current upper-computer software intentionally rejects unverified packets.
 */
#ifndef WIFI_IMAGE_TRANSFER_ENABLE_PAYLOAD_CRC
#define WIFI_IMAGE_TRANSFER_ENABLE_PAYLOAD_CRC     (1u)
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

#if (WIFI_IMAGE_TRANSFER_LOG_FIFO_SIZE < 2u) || \
    (WIFI_IMAGE_TRANSFER_LOG_FIFO_SIZE > 65535u)
#error "WIFI_IMAGE_TRANSFER_LOG_FIFO_SIZE must be in the range 2..65535."
#endif

#if (WIFI_IMAGE_TRANSFER_RGB565_MSB_FIRST != 0u) && \
    (WIFI_IMAGE_TRANSFER_RGB565_MSB_FIRST != 1u)
#error "WIFI_IMAGE_TRANSFER_RGB565_MSB_FIRST must be 0 or 1."
#endif

#if (WIFI_IMAGE_TRANSFER_ENABLE_PAYLOAD_CRC != 0u) && \
    (WIFI_IMAGE_TRANSFER_ENABLE_PAYLOAD_CRC != 1u)
#error "WIFI_IMAGE_TRANSFER_ENABLE_PAYLOAD_CRC must be 0 or 1."
#endif

/* Call after IPS/camera setup.  In DISABLED mode this is a no-op. */
void WifiImageTransfer_Init(void);

/*
 * Send one completed RGB565 frame after vision, PPU preparation, display and
 * telemetry work.  This is a synchronous, maximum-throughput call: the source
 * image must remain valid until it returns, and CPU0 waits until every SPI
 * fragment has been submitted to the WiFi module.
 * Only ORIGINAL SCC8660_W x SCC8660_H frames of SCC8660_IMAGE_SIZE bytes are
 * accepted; NULL/wrong-size/not-ready calls are ignored. Do not pass RGB888
 * colour masks or differently sized IPM output. This call sends, not processes.
 */
void WifiImageTransfer_SubmitRgb565Frame(const uint16 *image, uint32 image_bytes);

/*
 * Queue an already assembled debug line for a type-2 packet.  It is normally
 * called through WifiImageTransfer_PutChar() by the printf redirection hook.
 * The call performs no SPI transaction. Returns 1 for a non-empty input,
 * NOT an acknowledgement of delivery; a full FIFO drops newest bytes.
 * The FIFO is not multi-producer synchronized: serialize logging on CPU0;
 * do not concurrently call from multiple cores or interrupt writers.
 */
uint8 WifiImageTransfer_SubmitText(const char *text, uint16 text_length);

/*
 * printf redirection entry points.  Call WifiImageTransfer_WriteLogBytes()
 * from the project's existing TriCore GCC write() hook (commonly located in
 * zf_common_debug.c), so ordinary printf output is sent through this WiFi TCP
 * stream without changing each application call site.
 *
 * A CR is ignored.  Service() groups queued bytes into a type-2 packet at a
 * LF or at the safe text-packet size limit, so ordinary calls such as
 * printf("[info] Base_StartWaitingIMU\\n"); require no application change.
 */
void WifiImageTransfer_PutChar(char character);
void WifiImageTransfer_WriteLogBytes(const char *data, uint32 length);

/* Send at most one SPI fragment of queued printf text.  Call once per CPU0
 * main-loop iteration; it returns immediately while the WiFi SPI bus is busy.
 */
void WifiImageTransfer_Service(void);
/* Compatibility alias for ONE Service iteration, not a blocking drain. */
void WifiImageTransfer_FlushLog(void);

/* 1 after a successful connection, otherwise 0; no probing is performed. */
uint8 WifiImageTransfer_IsReady(void);

#endif /* CODE_WIFI_IMAGE_TRANSFER_H_ */
