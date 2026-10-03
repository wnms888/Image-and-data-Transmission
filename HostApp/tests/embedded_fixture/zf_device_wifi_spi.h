/* Desktop test SPI shim records packets instead of using hardware. */
#include "Header.h"
#define WIFI_SPI_AUTO_CONNECT 0
#define WIFI_SPI_LOCAL_PORT "6666"
uint8 wifi_spi_init(const char *ssid, const char *password);
uint8 wifi_spi_wifi_connect(const char *ssid, const char *password);
uint8 wifi_spi_socket_connect(const char *kind, const char *ip, const char *port, const char *local);
uint32 wifi_spi_stream_send_buffer(const uint8 *data, uint32 length);
