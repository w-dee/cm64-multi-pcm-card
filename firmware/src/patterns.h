#ifndef CM64_DIAG_PATTERNS_H
#define CM64_DIAG_PATTERNS_H
#include <stdint.h>
#define NOR_BYTES 0x800000u
#define NOR_MASK (NOR_BYTES - 1u)
#define NOR_BANK_BYTES 0x80000u
#define NOR_BANKS 16u
#define NOR_TOP_START 0x7f0000u

static inline uint32_t nor_sector_bytes(uint32_t address) {
    return address < NOR_TOP_START ? 0x10000u : 0x2000u;
}
#define DIAG_POINTS 68u
#define DIAG_SEED 0x64c0ffeeu

static inline uint8_t diag_pattern(uint32_t address, unsigned invert) {
    uint32_t x = address ^ DIAG_SEED;
    x ^= x >> 16;
    x *= 0x7feb352du;
    x ^= x >> 15;
    x *= 0x846ca68bu;
    x ^= x >> 16;
    return (uint8_t)(x ^ (invert ? 0xffu : 0u));
}

static inline uint32_t diag_address(unsigned index) {
    if (index == 0) return 0;
    if (index == 1) return NOR_MASK;
    if (index < 25) return 1u << (index - 2);
    if (index < 48) return NOR_MASK ^ (1u << (index - 25));
    return 0x123400u + index - 48;
}

static inline uint8_t diag_value(unsigned index) {
    if (index < 48) return (uint8_t)(0x20u + index);
    index -= 48;
    if (index == 0) return 0x00;
    if (index == 1) return 0xff;
    if (index == 2) return 0x55;
    if (index == 3) return 0xaa;
    if (index < 12) return (uint8_t)(1u << (index - 4));
    return (uint8_t)~(1u << (index - 12));
}
#endif
