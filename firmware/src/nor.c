#include <stdio.h>
#include <string.h>
#include "pico/stdlib.h"
#include "pico/stdio_usb.h"
#include "hardware/clocks.h"
#include "hardware/pio.h"
#include "hardware/sync.h"
#include "tusb.h"
#include "flash_shift.pio.h"
#include "nor.h"
#include "patterns.h"

#define STROBES ((1u << PCM_CE_N_PIN) | (1u << PCM_OE_N_PIN) | (1u << PCM_WE_N_PIN))
static PIO const shifter = pio0;
static unsigned sm;
static bool armed, initialized, io_fault;
static int bus_mode;  // 0 = isolated, 1 = read, 2 = write
static const char *reason = "none";
static const char *stage = "idle";
static uint32_t checked, mismatches, first_bad, crc_table[256];
static uint8_t first_expected, first_actual;
static int last_result = -1;
static uint64_t heartbeat_at;

static void idle(void) { gpio_set_mask(STROBES); }
static void isolate(void) {
    idle();
    gpio_put(PCM_DATA_OE_N_PIN, true);
    busy_wait_us_32(1);  // Buffer/Flash disable, including external gate delays.
    gpio_set_dir_in_masked(0xffu);
    gpio_put(PCM_DATA_DIR_PIN, false);
    bus_mode = 0;
}
void flash_diag_off(void) {
    isolate();
    gpio_put(PCM_ARM_PIN, false);
    armed = false;
    gpio_put(PCM_LED_GREEN_PIN, false);
}
static void output(uint pin, bool high) {
    gpio_init(pin);
    gpio_put(pin, high);
    gpio_set_dir(pin, GPIO_OUT);
}
static bool environment_ok(void) {
    return stdio_usb_connected() && !tud_suspended() &&
           gpio_get(PCM_USB_PRESENT_PIN) && !gpio_get(PCM_HOST_PRESENT_PIN) &&
           gpio_get(PCM_SYS_READY_PIN) && gpio_get(PCM_USB_FAULT_N_PIN);
}
static bool check(void) {
    if (io_fault) return false;
    if (!environment_ok() || (armed && gpio_get(PCM_PROG_EN_N_PIN))) {
        reason = "interlock-or-USB-disconnect";
        flash_diag_off();
        return false;
    }
    return true;
}
static void progress(uint32_t address) {
    uint64_t now = time_us_64();
    if (now >= heartbeat_at) {
        printf("PROGRESS stage=%s address=%06lx checked=%lu mismatches=%lu\n",
               stage, (unsigned long)address, (unsigned long)checked, (unsigned long)mismatches);
        heartbeat_at = now + 1000000;
    }
}
static bool address(uint32_t value) {
    idle();  // All address changes occur with CE/OE/WE inactive.
    if (value >= NOR_BYTES || io_fault) return false;
    pio_sm_put(shifter, sm, value);  // LSB first; bit 23 is the spare zero.
    uint64_t end = time_us_64() + 1000;
    while (pio_sm_is_rx_fifo_empty(shifter, sm)) {
        if (time_us_64() >= end) {
            io_fault = true;
            reason = "address-PIO-timeout";
            flash_diag_off();
            return false;
        }
    }
    (void)pio_sm_get(shifter, sm);
    gpio_put(PCM_ADDR_LATCH_PIN, true);
    busy_wait_at_least_cycles(16);
    gpio_put(PCM_ADDR_LATCH_PIN, false);
    busy_wait_at_least_cycles(32);
    return true;
}
static void direction(int mode, uint8_t data) {
    if (bus_mode == mode) return;
    isolate();
    if (mode == 2) {
        gpio_put(PCM_DATA_DIR_PIN, true);
        gpio_put_masked(0xffu, data);
        gpio_set_dir_out_masked(0xffu);
    }
    busy_wait_us_32(1);
    gpio_put(PCM_DATA_OE_N_PIN, false);
    busy_wait_us_32(1);
    bus_mode = mode;
}
static uint8_t read_byte(uint32_t a) {
    direction(1, 0);
    if (!address(a)) return 0;
    gpio_clr_mask((1u << PCM_CE_N_PIN) | (1u << PCM_OE_N_PIN));
    busy_wait_us_32(1);  // Flash access + both translators + PCB margin.
    uint8_t value = (uint8_t)gpio_get_all();
    idle();
    return value;
}
static bool write_byte(uint32_t a, uint8_t d) {
    direction(2, d);
    if (!address(a)) return false;
    gpio_put_masked(0xffu, d);
    // No USB handler or reset can interrupt an active WE pulse.
    uint32_t irq = save_and_disable_interrupts();
    gpio_put(PCM_CE_N_PIN, false);
    busy_wait_at_least_cycles(32);
    gpio_put(PCM_WE_N_PIN, false);
    busy_wait_at_least_cycles(64);
    gpio_put(PCM_WE_N_PIN, true);
    busy_wait_at_least_cycles(64);
    gpio_put(PCM_CE_N_PIN, true);
    restore_interrupts(irq);
    return true;
}
static bool unlock(uint8_t command) {
    return write_byte(0xaaa, 0xaa) && write_byte(0x555, 0x55) && write_byte(0xaaa, command);
}
static bool reset_array(void) {
    // Also exits a write-buffer-abort state.
    return unlock(0xf0);
}
static uint8_t status_register(void) {
    if (!write_byte(0xaaa, 0x70)) return 0;
    return read_byte(0);  // x8: even address selects SR7:0, overlay lasts one read.
}
static bool wait_ready(uint32_t timeout_ms, uint32_t a) {
    uint64_t end = time_us_64() + (uint64_t)timeout_ms * 1000;
    while (time_us_64() < end) {
        if (!check()) return false;
        uint8_t sr = status_register();
        if (io_fault) return false;
        if (sr & 0x80) {
            if (sr & 0x7e) {
                printf("ERROR status=%02x address=%06lx\n", sr, (unsigned long)a);
                reason = "flash-status-error";
                return false;
            }
            return true;
        }
        progress(a);
        busy_wait_us_32(20);
    }
    reason = "flash-operation-timeout";
    return false;
}
static bool begin(void) {
    reason = "none";
    io_fault = false;
    if (!initialized || !check()) return false;
    gpio_put(PCM_LED_GREEN_PIN, true);
    if (!address(0)) return false;
    gpio_put(PCM_ARM_PIN, true);
    armed = true;
    busy_wait_us_32(200);
    return check() && reset_array();
}
static bool identify(void) {
    if (!unlock(0x90)) return false;
    unsigned maker = read_byte(0), id1 = read_byte(2), id2 = read_byte(0x1c), id3 = read_byte(0x1e);
    printf("JEDEC manufacturer=%02x device=%02x,%02x,%02x\n", maker, id1, id2, id3);
    bool ok = maker == 1 && id1 == 0x7e && id2 == 0x10 && id3 == 1;
    if (!reset_array() || !write_byte(0xaa, 0x98)) return false;
    unsigned q = read_byte(0x20), r = read_byte(0x22), y = read_byte(0x24);
    unsigned size = read_byte(0x4e), buf = read_byte(0x54), regions = read_byte(0x58);
    unsigned cmd = read_byte(0x26) | ((unsigned)read_byte(0x28) << 8);
    printf("CFI QRY=%02x,%02x,%02x size_exp=%u buffer_exp=%u regions=%u command_set=%u\n",
           q,r,y,size,buf,regions,cmd);
    ok = ok && q == 'Q' && r == 'R' && y == 'Y' && size == 23 && buf == 8 && regions == 2 && cmd == 2;
    // CFI lists boot-region geometry first, also for the top-boot model.
    const uint8_t geometry[] = {7, 0, 0x20, 0, 0x7e, 0, 0, 1};
    for (unsigned i = 0; i < sizeof geometry; ++i)
        if (read_byte(0x5a + 2*i) != geometry[i]) ok = false;
    if (!reset_array()) return false;
    if (!ok) reason = "unexpected-JEDEC-or-CFI";
    return ok && check();
}
bool flash_diag_identify(void) {
    bool ok = begin() && identify();
    if (armed && environment_ok()) (void)reset_array();
    flash_diag_off();
    printf("IDENTIFY result=%s reason=%s ARM=0 PROG_EN_N=%u\n", ok ? "PASS" : "FAIL", reason,
           gpio_get(PCM_PROG_EN_N_PIN));
    return ok;
}
static bool erase(void) {
    if (!check()) return false;
    printf("ERASE begin chip_bytes=%u\n", NOR_BYTES);
    if (!write_byte(0xaaa,0x71) || !unlock(0x80) || !unlock(0x10)) return false;
    bool ok = wait_ready(180000, 0);
    if (ok) printf("ERASE complete status=80\n");
    return ok;
}
static bool program_page(uint32_t a, const uint8_t *data) {
    if ((a & 255u) || a >= NOR_BYTES || !check()) return false;
    // 256-byte aligned pages fit every sector, including the 8 KiB top sectors.
    if (!write_byte(0xaaa,0xaa) || !write_byte(0x555,0x55) ||
        !write_byte(a,0x25) || !write_byte(a,0xff)) return false;
    for(unsigned i=0;i<256;++i) if(!write_byte(a+i,data[i]))return false;
    if(!write_byte(a,0x29) || !wait_ready(100, a+255))return false;
    uint8_t actual=read_byte(a+255);
    if(actual!=data[255]) {
        printf("ERROR program_tail address=%06lx expected=%02x actual=%02x\n",(unsigned long)(a+255),data[255],actual);
        reason="program-tail-mismatch";
        return false;
    }
    return !io_fault;
}

static void mismatch(uint32_t a, uint8_t expected, uint8_t actual) {
    if (!mismatches) { first_bad=a;first_expected=expected;first_actual=actual; }
    ++mismatches;
    if (mismatches <= 8) printf("MISMATCH address=%06lx expected=%02x actual=%02x\n", (unsigned long)a,expected,actual);
}
static uint32_t crc_byte(uint32_t crc, uint8_t byte) { return crc_table[(crc ^ byte) & 255u] ^ (crc >> 8); }
static bool verify_all(int pattern, bool reverse) {
    checked = mismatches = 0;
    uint32_t actual_crc = 0xffffffffu, expected_crc = 0xffffffffu;
    printf("VERIFY begin pattern=%d order=%s bytes=%u\n",pattern,reverse?"descending":"ascending",NOR_BYTES);
    for (uint32_t i=0;i<NOR_BYTES;++i) {
        uint32_t a = reverse ? NOR_MASK - i : i;
        if (!(i & 4095u)) { if (!check()) return false; progress(a); }
        uint8_t expected = pattern < 0 ? 0xff : diag_pattern(a,(unsigned)pattern);
        uint8_t actual = read_byte(a);
        if (io_fault) return false;
        actual_crc = crc_byte(actual_crc, actual);
        expected_crc = crc_byte(expected_crc, expected);
        if (actual != expected) mismatch(a,expected,actual);
        ++checked;
    }
    printf("VERIFY end pattern=%d order=%s checked=%lu mismatches=%lu crc=%08lx expected_crc=%08lx\n",
           pattern,reverse?"descending":"ascending",(unsigned long)checked,(unsigned long)mismatches,
           (unsigned long)(actual_crc^0xffffffffu),(unsigned long)(expected_crc^0xffffffffu));
    if (mismatches) reason="readback-mismatch";
    return mismatches == 0;
}
static bool sparse_test(void) {
    stage="bus-pattern-program";
    // Program each buffer page only once to preserve normal ECC operation.
    for (unsigned i=0;i<DIAG_POINTS;++i) {
        uint32_t page=diag_address(i)&~255u;
        bool seen=false;
        for(unsigned j=0;j<i;++j) if((diag_address(j)&~255u)==page)seen=true;
        if(seen)continue;
        uint8_t data[256];memset(data,0xff,sizeof data);
        for(unsigned j=0;j<DIAG_POINTS;++j) if((diag_address(j)&~255u)==page)data[diag_address(j)&255u]=diag_value(j);
        if(!program_page(page,data))return false;
    }
    mismatches=checked=0;
    stage="bus-pattern-verify";
    for(unsigned pass=0;pass<2;++pass) for(unsigned k=0;k<DIAG_POINTS;++k) {
        unsigned i=pass?DIAG_POINTS-1-k:k;
        uint8_t value=read_byte(diag_address(i));
        if(value!=diag_value(i))mismatch(diag_address(i),diag_value(i),value);
        ++checked;
    }
    printf("BUS_TEST points=%u reads=%lu mismatches=%lu\n",DIAG_POINTS,(unsigned long)checked,(unsigned long)mismatches);
    if(mismatches)reason="bus-pattern-mismatch";
    return !mismatches && check();
}
bool flash_diag_run(void) {
    uint64_t started=time_us_64();
    last_result=-1;checked=mismatches=0;heartbeat_at=0;stage="identify";
    puts("TEST begin version=1.0.0 destructive=1 bytes=8388608 seed=64c0ffee");
    bool ok=begin() && identify();
    if(ok) {stage="initial-erase";ok=erase();}
    if(ok) {stage="initial-erased-verify";ok=verify_all(-1,false);}
    if(ok)ok=sparse_test();
    for(unsigned pattern=0;ok && pattern<2;++pattern) {
        stage=pattern?"invert-erase":"hash-erase";ok=erase();
        if(ok){stage=pattern?"invert-erased-verify":"hash-erased-verify";ok=verify_all(-1,false);}
        if(ok) {
            stage=pattern?"invert-program":"hash-program";
            checked=mismatches=0;
            printf("PROGRAM begin pattern=%u bytes=%u\n",pattern,NOR_BYTES);
            for(uint32_t a=0;ok && a<NOR_BYTES;a+=256) {
                uint8_t data[256];
                for(unsigned i=0;i<256;++i)data[i]=diag_pattern(a+i,pattern);
                ok=program_page(a,data);
                if(ok)checked+=256;
                progress(a);
            }
            printf("PROGRAM end pattern=%u written=%lu result=%s\n",pattern,(unsigned long)checked,ok?"PASS":"FAIL");
        }
        if(ok){stage=pattern?"invert-verify":"hash-verify";ok=verify_all((int)pattern,pattern!=0);}
    }
    if(armed && environment_ok() && !reset_array())ok=false;
    flash_diag_off();busy_wait_us_32(200);
    if(!gpio_get(PCM_PROG_EN_N_PIN)){ok=false;reason="buffer-disable-not-confirmed";}
    last_result=ok?1:0;
    printf("RESULT %s stage=%s reason=%s elapsed_ms=%llu ARM=0 PROG_EN_N=%u\n",
           ok?"PASS":"FAIL",stage,reason,(unsigned long long)((time_us_64()-started)/1000),gpio_get(PCM_PROG_EN_N_PIN));
    if(mismatches)printf("FIRST_MISMATCH address=%06lx expected=%02x actual=%02x\n",(unsigned long)first_bad,first_expected,first_actual);
    stage="idle";
    return ok;
}
void flash_diag_status(void) {
    printf("DIAG version=1.0.0 last_result=%d ARM=%u USB=%u HOST=%u READY=%u FAULT_N=%u PROG_EN_N=%u RY_BY_N=%u GREEN=%u RED=%u\n",
           last_result,gpio_get(PCM_ARM_PIN),gpio_get(PCM_USB_PRESENT_PIN),gpio_get(PCM_HOST_PRESENT_PIN),
           gpio_get(PCM_SYS_READY_PIN),gpio_get(PCM_USB_FAULT_N_PIN),gpio_get(PCM_PROG_EN_N_PIN),gpio_get(PCM_RY_BY_N_PIN),
           gpio_get(PCM_LED_GREEN_PIN),gpio_get(PCM_LED_RED_PIN));
}

bool nor_begin(void) {
    stage = "transfer";
    heartbeat_at = 0;
    bool ok = begin() && identify();
    if (ok) printf("ACCESS ARM=1 GREEN=%u RED=%u\n",
                   gpio_get(PCM_LED_GREEN_PIN), gpio_get(PCM_LED_RED_PIN));
    return ok;
}

bool nor_check(void) { return check(); }
const char *nor_error(void) { return reason; }

uint32_t nor_crc(uint32_t crc, const uint8_t *data, size_t bytes) {
    for (size_t i = 0; i < bytes; ++i) crc = crc_byte(crc, data[i]);
    return crc;
}

bool nor_read(uint32_t a, uint8_t *data, size_t bytes) {
    if (!bytes || a >= NOR_BYTES || bytes > NOR_BYTES - a || !check()) return false;
    for (size_t i = 0; i < bytes; ++i) {
        data[i] = read_byte(a + i);
        if (io_fault) return false;
    }
    return true;
}

bool nor_erase(uint32_t a, uint32_t bytes) {
    // Only complete banks or the whole device; no shared erase sectors.
    if (a % NOR_BANK_BYTES || !bytes || bytes % NOR_BANK_BYTES ||
        a >= NOR_BYTES || bytes > NOR_BYTES - a) {
        reason = "invalid-erase-range";
        return false;
    }
    stage = "erase";
    if (a == 0 && bytes == NOR_BYTES) {
        if (!erase()) return false;
    } else {
        for (uint32_t p = a; p < a + bytes; p += nor_sector_bytes(p)) {
            printf("SECTOR_ERASE address=%06lx bytes=%lu\n", (unsigned long)p,
                   (unsigned long)nor_sector_bytes(p));
            if (!check() || !write_byte(0xaaa, 0x71) || !unlock(0x80) ||
                !write_byte(0xaaa, 0xaa) || !write_byte(0x555, 0x55) ||
                !write_byte(p, 0x30)) return false;
            busy_wait_us_32(100);  // Sector collection window, before status polling.
            if (!wait_ready(10000, p)) return false;
        }
    }
    stage = "erased-verify";
    uint8_t data[256];
    for (uint32_t p = a; p < a + bytes; p += sizeof data) {
        if (!nor_read(p, data, sizeof data)) return false;
        for (unsigned i = 0; i < sizeof data; ++i) {
            if (data[i] != 0xff) { reason = "not-erased"; return false; }
        }
        progress(p);
    }
    stage = "program";
    return true;
}

bool nor_program(uint32_t a, const uint8_t data[256]) {
    if (!program_page(a, data)) return false;
    uint8_t actual[256];
    if (!nor_read(a, actual, sizeof actual)) return false;
    if (memcmp(data, actual, sizeof actual)) {
        reason = "page-readback-mismatch";
        return false;
    }
    return true;
}

bool nor_end(bool ok) {
    if (armed && environment_ok() && !reset_array()) ok = false;
    flash_diag_off();
    busy_wait_us_32(200);
    if (!gpio_get(PCM_PROG_EN_N_PIN)) {
        reason = "buffer-disable-not-confirmed";
        ok = false;
    }
    stage = "idle";
    return ok;
}

void flash_diag_init(void) {
    for(unsigned pin=0;pin<8;++pin){gpio_init(pin);gpio_disable_pulls(pin);}
    output(PCM_ARM_PIN,false);output(PCM_DATA_OE_N_PIN,true);
    output(PCM_CE_N_PIN,true);output(PCM_OE_N_PIN,true);output(PCM_WE_N_PIN,true);
    output(PCM_DATA_DIR_PIN,false);output(PCM_ADDR_SER_PIN,false);
    output(PCM_ADDR_CLK_PIN,false);output(PCM_ADDR_LATCH_PIN,false);
    output(PCM_LED_GREEN_PIN,false);output(PCM_LED_RED_PIN,true);
    const unsigned monitors[]={PCM_RY_BY_N_PIN,PCM_HOST_PRESENT_PIN,PCM_USB_PRESENT_PIN,PCM_SYS_READY_PIN,PCM_PROG_EN_N_PIN,PCM_USB_FAULT_N_PIN};
    for(unsigned i=0;i<count_of(monitors);++i){gpio_init(monitors[i]);gpio_disable_pulls(monitors[i]);gpio_set_input_hysteresis_enabled(monitors[i],true);}
    sm=(unsigned)pio_claim_unused_sm(shifter,true);
    unsigned offset=pio_add_program(shifter,&flash_shift_program);
    pio_sm_config config=flash_shift_program_get_default_config(offset);
    sm_config_set_out_pins(&config,PCM_ADDR_SER_PIN,1);
    sm_config_set_sideset_pins(&config,PCM_ADDR_CLK_PIN);
    sm_config_set_out_shift(&config,true,false,32);
    sm_config_set_clkdiv(&config,(float)clock_get_hz(clk_sys)/10000000.0f);
    pio_gpio_init(shifter,PCM_ADDR_SER_PIN);pio_gpio_init(shifter,PCM_ADDR_CLK_PIN);
    pio_sm_set_consecutive_pindirs(shifter,sm,PCM_ADDR_SER_PIN,2,true);
    pio_sm_init(shifter,sm,offset,&config);pio_sm_set_enabled(shifter,sm,true);
    for(unsigned i=0;i<256;++i){uint32_t crc=i;for(unsigned bit=0;bit<8;++bit)crc=(crc>>1)^((crc&1)?0xedb88320u:0);crc_table[i]=crc;}
    initialized=true;
}
