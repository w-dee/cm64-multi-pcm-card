cmake_minimum_required(VERSION 3.17)
set(PICO_BOARD cm64_pcm_card CACHE STRING "Target board")
list(APPEND PICO_BOARD_HEADER_DIRS "${CMAKE_CURRENT_LIST_DIR}/boards")
if(NOT PICO_SDK_PATH AND DEFINED ENV{PICO_SDK_PATH})
    set(PICO_SDK_PATH "$ENV{PICO_SDK_PATH}")
endif()
if(NOT EXISTS "${PICO_SDK_PATH}/external/pico_sdk_import.cmake")
    message(FATAL_ERROR "Set PICO_SDK_PATH to Pico SDK 2.3.1 (including TinyUSB)")
endif()
include("${PICO_SDK_PATH}/external/pico_sdk_import.cmake")
project(cm64_pcm_card VERSION 1.0.0 LANGUAGES C CXX ASM)
set(CMAKE_C_STANDARD 11)
set(CMAKE_CXX_STANDARD 17)
pico_sdk_init()
if(NOT PICO_SDK_VERSION_STRING VERSION_EQUAL "2.3.1")
    message(FATAL_ERROR "This firmware requires Pico SDK 2.3.1")
endif()
add_executable(pcm_card src/main.c src/nor.c)
pico_generate_pio_header(pcm_card "${CMAKE_CURRENT_LIST_DIR}/src/flash_shift.pio")
target_link_libraries(pcm_card PRIVATE pico_stdlib hardware_pio hardware_clocks)
target_compile_options(pcm_card PRIVATE -Wall -Wextra -Werror)
target_compile_definitions(pcm_card PRIVATE
    USBD_PRODUCT="CM-64 PCM Card"
    PICO_STDIO_USB_DEVICE_SELF_POWERED=0
    PICO_STDIO_USB_CONNECT_WAIT_TIMEOUT_MS=0
    PICO_STDIO_USB_STDOUT_TIMEOUT_US=1000000
    PICO_STDIO_USB_ENABLE_CRLF_SUPPORT=0
    PICO_ENABLE_USB_RESET_VIA_BAUD_RATE=0
    PICO_ENABLE_USB_RESET_VIA_VENDOR_INTERFACE=0
)
pico_enable_stdio_usb(pcm_card 1)
pico_enable_stdio_uart(pcm_card 0)
pico_set_program_name(pcm_card "cm64-pcm-card")
pico_set_program_version(pcm_card "${PROJECT_VERSION}")
pico_add_extra_outputs(pcm_card)
