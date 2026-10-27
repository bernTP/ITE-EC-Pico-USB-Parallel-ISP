cmake_minimum_required(VERSION 3.14)

set(PICO_BOARD pico CACHE STRING "Board type")

include(pico_sdk_import.cmake)

project(ite_pp_isp C CXX ASM)

set(CMAKE_C_STANDARD 11)

pico_sdk_init()

add_executable(ite_pp_isp
    main.c
    lines.c
    epp.c
    usb_descriptors.c
)

target_compile_options(ite_pp_isp PRIVATE -Wall -Wextra -Wno-unused-parameter)

target_include_directories(ite_pp_isp PUBLIC
        ${CMAKE_CURRENT_LIST_DIR})

target_link_libraries(ite_pp_isp PRIVATE
    pico_stdlib
    pico_unique_id
    hardware_gpio
    hardware_clocks
    tinyusb_device
    tinyusb_board
)

pico_enable_stdio_uart(ite_pp_isp 0)
pico_enable_stdio_usb(ite_pp_isp 0)

pico_add_extra_outputs(ite_pp_isp)
