/* Desktop parity-test shim ONLY. Never add this directory to board includes. */
#ifndef IMGT_DESKTOP_TEST_HEADER_H
#define IMGT_DESKTOP_TEST_HEADER_H
#include <stdint.h>
#include <stdbool.h>
#include <stddef.h>
#include <string.h>
#include <math.h>
#include <stdio.h>
typedef uint8_t uint8;
typedef uint16_t uint16;
typedef uint32_t uint32;
#ifndef SCC8660_W
#define SCC8660_W 160
#endif
#ifndef SCC8660_H
#define SCC8660_H 128
#endif
#define SCC8660_IMAGE_SIZE ((uint32)SCC8660_W*SCC8660_H*2u)
extern uint16 scc8660_image[SCC8660_H][SCC8660_W];
extern uint8 scc8660_finish_flag;
uint8 scc8660_set_white_balance(uint16 value);
void ips200_show_string(int x, int y, const char *text);
void system_delay_ms(int milliseconds);
#endif
