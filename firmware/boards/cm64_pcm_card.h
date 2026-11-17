// Board: pcm-card/01_mcu.kicad_sch (RP2040, 12 MHz, W25Q128JVPIQ).
// This file is also included by the assembler; use preprocessor directives only.
#ifndef _BOARDS_CM64_PCM_CARD_H
#define _BOARDS_CM64_PCM_CARD_H

pico_board_cmake_set(PICO_PLATFORM, rp2040)
pico_board_cmake_set_default(PICO_FLASH_SIZE_BYTES, (16 * 1024 * 1024))
#define PICO_FLASH_SIZE_BYTES (16 * 1024 * 1024)
#define PICO_XOSC_FREQ_HZ 12000000
#define PICO_BOOT_STAGE2_CHOOSE_W25Q080 1
#define PICO_FLASH_SPI_CLKDIV 4

#define PCM_DATA_DIR_PIN 8
#define PCM_DATA_OE_N_PIN 9
#define PCM_CE_N_PIN 10
#define PCM_OE_N_PIN 11
#define PCM_WE_N_PIN 12
#define PCM_ARM_PIN 13
#define PCM_RY_BY_N_PIN 14
#define PCM_HOST_PRESENT_PIN 15
#define PCM_ADDR_SER_PIN 16
#define PCM_ADDR_CLK_PIN 17
#define PCM_ADDR_LATCH_PIN 18
#define PCM_LED_GREEN_PIN 19
#define PCM_LED_RED_PIN 20
#define PCM_USB_PRESENT_PIN 23
#define PCM_SYS_READY_PIN 24
#define PCM_PROG_EN_N_PIN 26
#define PCM_USB_FAULT_N_PIN 27

#endif
