// Native fault-model checks for the patterns actually compiled into the MCU.
#include <assert.h>
#include <stdio.h>
#include "../src/patterns.h"

static uint32_t mapping(uint32_t a,unsigned kind,unsigned x,unsigned y) {
    unsigned bx=(a>>x)&1u,by=(a>>y)&1u;
    if(kind==0)return a&~(1u<<x);
    if(kind==1)return a|(1u<<x);
    unsigned value=kind==2?(bx&by):(bx|by);
    return (a&~((1u<<x)|(1u<<y)))|(value<<x)|(value<<y);
}
static unsigned detects_alias(unsigned kind,unsigned x,unsigned y) {
    for(unsigned i=0;i<48;++i)for(unsigned j=0;j<i;++j)
        if(mapping(diag_address(i),kind,x,y)==mapping(diag_address(j),kind,x,y) && diag_value(i)!=diag_value(j))return 1;
    return 0;
}
static uint32_t crc32(uint32_t crc,uint8_t byte) {
    crc^=byte;
    for(unsigned i=0;i<8;++i)crc=(crc>>1)^((crc&1)?0xedb88320u:0);
    return crc;
}
static void check_595_order(uint32_t a) {
    // SER enters U8.Q0; U8.Q7 -> U9.SER; U9.Q7 -> U10.SER.
    uint32_t outputs=0;
    for(unsigned bit=0;bit<24;++bit)outputs=((outputs<<1)|((a>>bit)&1u))&0xffffffu;
    assert((outputs&1u)==0); // U8.Q0 is the unused output.
    uint32_t reconstructed=0;
    for(unsigned bit=0;bit<23;++bit)reconstructed|=((outputs>>(23-bit))&1u)<<bit;
    assert(reconstructed==a);
}
int main(void) {
    unsigned total_sectors = 0;
    for (unsigned bank = 0; bank < NOR_BANKS; ++bank) {
        uint32_t end = (bank + 1) * NOR_BANK_BYTES;
        uint32_t a = bank * NOR_BANK_BYTES;
        unsigned sectors = 0;
        while (a < end) {
            uint32_t bytes = nor_sector_bytes(a);
            assert(a % bytes == 0 && a + bytes <= end);
            a += bytes;
            ++sectors;
        }
        assert(a == end);
        assert(sectors == (bank == 15 ? 15u : 8u));
        total_sectors += sectors;
    }
    assert(total_sectors == 135);
    for(unsigned i=0;i<DIAG_POINTS;++i) {
        assert(diag_address(i)<NOR_BYTES);
        for(unsigned j=0;j<i;++j)assert(diag_address(i)!=diag_address(j));
    }
    unsigned address_faults=0,data_faults=0;
    for(unsigned x=0;x<23;++x) {
        assert(detects_alias(0,x,0));assert(detects_alias(1,x,0));address_faults+=2;
        for(unsigned y=0;y<x;++y) {
            assert(detects_alias(2,x,y));assert(detects_alias(3,x,y));address_faults+=2;
        }
    }
    for(unsigned kind=0;kind<4;++kind)for(unsigned x=0;x<8;++x)for(unsigned y=0;y<(kind<2?1:x);++y) {
        unsigned detected=0;
        for(unsigned i=48;i<DIAG_POINTS;++i)
            if(mapping(diag_value(i),kind,x,y)!=diag_value(i))detected=1;
        assert(detected);++data_faults;
    }
    // A consistent permutation remains one-to-one and cancels on roundtrip.
    for(unsigned i=0;i<48;++i)for(unsigned j=0;j<i;++j) {
        uint32_t a=diag_address(i),b=diag_address(j);
        uint32_t pa=(a&~3u)|((a&1)<<1)|((a&2)>>1);
        uint32_t pb=(b&~3u)|((b&1)<<1)|((b&2)>>1);
        assert(pa!=pb);
    }
    for(unsigned i=48;i<DIAG_POINTS;++i) {
        unsigned d=diag_value(i),p=(d&~3u)|((d&1)<<1)|((d&2)>>1);
        assert(((p&~3u)|((p&1)<<1)|((p&2)>>1))==d);
    }
    uint32_t crc[3]={0xffffffffu,0xffffffffu,0xffffffffu};
    for(uint32_t a=0;a<NOR_BYTES;++a) {
        check_595_order(a);
        assert((diag_pattern(a,0)^diag_pattern(a,1))==0xff);
        crc[0]=crc32(crc[0],0xff);
        crc[1]=crc32(crc[1],diag_pattern(a,0));
        crc[2]=crc32(crc[2],diag_pattern(NOR_MASK-a,1));
    }
    printf("PASS address_fault_models=%u data_fault_models=%u full_bytes=%u\n",address_faults,data_faults,NOR_BYTES);
    printf("CRC erased=%08x hash_ascending=%08x invert_descending=%08x\n",crc[0]^0xffffffffu,crc[1]^0xffffffffu,crc[2]^0xffffffffu);
}
