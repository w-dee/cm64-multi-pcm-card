#ifndef CM64_NOR_H
#define CM64_NOR_H
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include "patterns.h"
void flash_diag_init(void);
void flash_diag_status(void);
bool flash_diag_identify(void);
bool flash_diag_run(void);
void flash_diag_off(void);
bool nor_begin(void);
bool nor_check(void);
bool nor_read(uint32_t address, uint8_t *data, size_t bytes);
bool nor_erase(uint32_t address, uint32_t bytes);
bool nor_program(uint32_t address, const uint8_t data[256]);
bool nor_end(bool ok);
const char *nor_error(void);
uint32_t nor_crc(uint32_t crc, const uint8_t *data, size_t bytes);
#endif
