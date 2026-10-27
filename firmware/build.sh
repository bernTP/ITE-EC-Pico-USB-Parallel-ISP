#!/usr/bin/env bash
# Build the firmware for Pico (RP2040) and Pico 2 (RP2350).
# Output: prebuilt/ite_pp_isp_<board>.uf2
#
# Needs PICO_SDK_PATH (default ~/pico/pico-sdk) and an arm-none-eabi
# toolchain on PATH or in PICO_TOOLCHAIN_PATH (default ~/pico/arm-gnu-toolchain).
set -euo pipefail

cd "$(dirname "$0")"

export PICO_SDK_PATH="${PICO_SDK_PATH:-$HOME/pico/pico-sdk}"
export PICO_TOOLCHAIN_PATH="${PICO_TOOLCHAIN_PATH:-$HOME/pico/arm-gnu-toolchain}"

BOARDS="${*:-pico pico2}"
mkdir -p prebuilt

for board in $BOARDS; do
    echo "=== $board ==="
    cmake -S . -B "build_$board" -DPICO_BOARD="$board" -DCMAKE_BUILD_TYPE=Release
    cmake --build "build_$board" -j"$(nproc)"
    cp "build_$board/ite_pp_isp.uf2" "prebuilt/ite_pp_isp_$board.uf2"
done

ls -l prebuilt/
