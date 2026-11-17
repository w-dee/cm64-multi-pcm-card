#include <stdio.h>
#include <string.h>
#include "pico/stdlib.h"
#include "pico/stdio_usb.h"
#include "pico/bootrom.h"
#include "nor.h"

// ASCII protocol 1: one request at a time, exact addresses, page CRCs,
// whole-transfer CRCs, and a final result after releasing the PROGRAM bus.
static bool line_read(char *line, size_t capacity, bool active) {
    size_t used = 0;
    bool overflow = false;
    uint64_t deadline = time_us_64() + 10000000;
    while (stdio_usb_connected() && (!active || nor_check())) {
        int c = getchar_timeout_us(1000);
        if (c == PICO_ERROR_TIMEOUT) {
            if (time_us_64() >= deadline) return false;
            continue;
        }
        if (c == '\r') continue;
        if (c == '\n') {
            line[used] = 0;
            return !overflow;
        }
        if (c < 32 || c > 126 || used + 1 >= capacity) overflow = true;
        else line[used++] = (char)c;
    }
    return false;
}

static void result(const char *command, bool ok, uint32_t bytes, uint32_t crc,
                   const char *error) {
    ok = nor_end(ok);
    printf("END %s %s bytes=%lu crc=%08lx ARM=0 PROG_EN_N=%u reason=%s\n",
           command, ok ? "OK" : "FAIL", (unsigned long)bytes, (unsigned long)crc,
           gpio_get(PCM_PROG_EN_N_PIN), ok ? "none" : (error ? error : nor_error()));
    stdio_flush();
}

static bool valid_range(uint32_t a, uint32_t bytes) {
    return a < NOR_BYTES && a % NOR_BANK_BYTES == 0 &&
           (bytes == NOR_BANK_BYTES || (a == 0 && bytes == NOR_BYTES)) &&
           bytes <= NOR_BYTES - a;
}

static void read_range(uint32_t base, uint32_t bytes) {
    if (!valid_range(base, bytes)) { result("READ", false, 0, 0, "invalid-range"); return; }
    bool ok = nor_begin();
    uint32_t done = 0, crc = 0xffffffffu;
    const char hex[] = "0123456789abcdef";
    while (ok && done < bytes) {
        uint8_t data[256];
        char encoded[513];
        ok = nor_read(base + done, data, sizeof data);
        if (!ok) break;
        for (unsigned i = 0; i < sizeof data; ++i) {
            encoded[2*i] = hex[data[i] >> 4];
            encoded[2*i+1] = hex[data[i] & 15];
        }
        encoded[512] = 0;
        crc = nor_crc(crc, data, sizeof data);
        printf("DATA %06lx %s %08lx\n", (unsigned long)(base + done), encoded,
               (unsigned long)(nor_crc(0xffffffffu, data, sizeof data) ^ 0xffffffffu));
        done += sizeof data;
    }
    result("READ", ok, done, crc ^ 0xffffffffu, NULL);
}

static int unhex(char c) {
    if (c >= '0' && c <= '9') return c - '0';
    if (c >= 'a' && c <= 'f') return c - 'a' + 10;
    if (c >= 'A' && c <= 'F') return c - 'A' + 10;
    return -1;
}

static void write_range(uint32_t base, uint32_t bytes, uint32_t expected_crc) {
    if (!valid_range(base, bytes)) { result("WRITE", false, 0, 0, "invalid-range"); return; }
    bool ok = nor_begin() && nor_erase(base, bytes);
    uint32_t done = 0, crc = 0xffffffffu;
    const char *error = NULL;
    if (ok) { puts("READY WRITE"); stdio_flush(); }
    while (ok && done < bytes) {
        char line[560], encoded[513];
        uint8_t data[256];
        unsigned long a, page_crc;
        int consumed = 0;
        if (!line_read(line, sizeof line, true)) {
            ok = false; error = "page-timeout-or-disconnect"; break;
        }
        if (sscanf(line, "PAGE %6lx %512s %8lx%n", &a, encoded, &page_crc, &consumed) != 3 ||
            !consumed || line[consumed] || a != base + done || strlen(encoded) != 512) {
            ok = false; error = "invalid-page-or-address"; break;
        }
        for (unsigned i = 0; i < sizeof data; ++i) {
            int hi = unhex(encoded[2*i]), lo = unhex(encoded[2*i+1]);
            if (hi < 0 || lo < 0) { ok = false; error = "invalid-page-hex"; break; }
            data[i] = (uint8_t)((hi << 4) | lo);
        }
        if (!ok) break;
        if ((nor_crc(0xffffffffu, data, sizeof data) ^ 0xffffffffu) != page_crc) {
            ok = false; error = "page-crc-mismatch"; break;
        }
        ok = nor_program(base + done, data);
        if (!ok) break;
        crc = nor_crc(crc, data, sizeof data);
        done += sizeof data;
        printf("ACK %06lx %08lx\n", a, page_crc);
        stdio_flush();
    }
    if (ok && (crc ^ 0xffffffffu) != expected_crc) {
        ok = false; error = "transfer-crc-mismatch";
    }
    result("WRITE", ok, done, crc ^ 0xffffffffu, error);
}

int main(void) {
    flash_diag_init();
    if (!stdio_init_all()) panic("USB initialization failed");
    while (true) {
        if (!stdio_usb_connected()) { flash_diag_off(); sleep_ms(10); continue; }
        char line[96];
        if (!line_read(line, sizeof line, false)) { flash_diag_off(); continue; }
        if (!line[0]) continue;
        unsigned long base, bytes, crc;
        int consumed = 0;
        if (!strcmp(line, "INFO")) {
            puts("INFO protocol=1 bytes=8388608 bank_bytes=524288 banks=16 page=256 version=1.0.0");
            flash_diag_status();
            result("INFO", flash_diag_identify(), 0, 0, NULL);
        } else if (!strcmp(line, "TEST ERASE_ALL")) {
            result("TEST", flash_diag_run(), 0, 0, NULL);
        } else if (!strcmp(line, "BOOTSEL")) {
            if (nor_end(true)) { puts("END BOOTSEL OK"); stdio_flush(); sleep_ms(100); reset_usb_boot(0, 0); }
        } else if (sscanf(line, "READ %6lx %6lx%n", &base, &bytes, &consumed) == 2 &&
                   consumed && !line[consumed]) {
            read_range(base, bytes);
        } else {
            consumed = 0;
            if (sscanf(line, "WRITE %6lx %6lx %8lx CONFIRM%n", &base, &bytes, &crc, &consumed) == 3 &&
                consumed && !line[consumed]) write_range(base, bytes, crc);
            else result("COMMAND", false, 0, 0, "unknown-command");
        }
    }
}
