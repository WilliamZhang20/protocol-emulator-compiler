#!/bin/sh
# End-to-end, local center-timer candidate experiment; does not modify RTL.
set -eu
: "${CIRCT_MAP:?Set CIRCT_MAP to the mapper executable or launcher}"
: "${CIRCT_OPT:?Set CIRCT_OPT to circt-opt executable or launcher}"
: "${SPATIAL_DIALECT:?Set SPATIAL_DIALECT to the built dialect plugin}"
HERE=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
RTL_ROOT=${RTL_ROOT:-"$HERE/../../protocol-emulator"}
RTL_ROOT=$(CDPATH= cd -- "$RTL_ROOT" && pwd)
SPATIAL_FRONTEND=${SPATIAL_FRONTEND:-"$HERE/../target/debug/spatial"}
VERILATOR=${VERILATOR:-verilator}
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT HUP INT TERM
python3 "$RTL_ROOT/experiments/grid_fabric/center_timer.py" --output "$TMP/candidate"
mkdir -p "$TMP/candidate/architecture"
cp "$TMP/candidate/grid_3x3.json" "$TMP/candidate/architecture/grid_3x3.json"
printf '%s\n' '{"timer0":4,"lut":2}' > "$TMP/layout.json"
for proto in spi uart; do
  if [ "$proto" = spi ]; then source="$HERE/examples/memory_spi.spt"; else source="$HERE/../examples/uart.spt"; fi
  "$SPATIAL_FRONTEND" frontend-mlir "$source" > "$TMP/$proto.mlir"
  "$CIRCT_MAP" "--compile-grid-$proto" "$TMP/$proto.mlir" "$TMP/$proto-base.mem" > "$TMP/$proto-base.json"
  python3 "$HERE/grid_workload.py" "$TMP/$proto-base.json" --contract "$RTL_ROOT/architecture/grid_3x3.json" > "$TMP/$proto-nets.json"
  python3 "$HERE/grid_route.py" "$TMP/$proto-nets.json" --contract "$RTL_ROOT/architecture/grid_3x3.json" --layout "$TMP/layout.json" --emit-mlir > "$TMP/$proto-route.mlir"
  "$CIRCT_OPT" "--load-dialect-plugin=$SPATIAL_DIALECT" "$TMP/$proto-route.mlir" -o /dev/null
  "$CIRCT_MAP" --route-grid "$TMP/$proto-route.mlir" > "$TMP/$proto-route.json"
  python3 "$HERE/grid_route_image.py" "$TMP/$proto-route.json" "$TMP/$proto-base.json" "$TMP/$proto-base.mem" "$TMP/$proto.mem" "$TMP/$proto.json" --contract "$TMP/candidate/grid_3x3.json"
  RTL_ROOT="$TMP/candidate" python3 "$HERE/test_grid_image.py" "$proto" "$TMP/$proto.mem" "$TMP/$proto.json"
done
unset VERILATOR_ROOT
cd "$RTL_ROOT"
"$VERILATOR" --binary --assert --timing -Wno-fatal -DFUNCTIONAL -DGRID_STREAM_ASSERT --top-module grid_stream_fabric_tb \
  --Mdir "$TMP/build" experiments/grid_stream/grid_stream_fabric_tb.sv src/grid_stream_fabric.v src/grid_stream_tiles.v \
  src/grid_stream_memory.v "$TMP/candidate/grid_core.v" src/grid_sequencer.v src/grid_switch.v src/fabric_v2_tiles.v src/fabric_crc.v src/fabric_shifter.v src/config_bank.v \
  src/memory_tiles.v src/program_memory.v test/models/RM_IHPSG13_1P_core_behavioral_bm_bist.v \
  test/models/RM_IHPSG13_1P_1024x8_c2_bm_bist.v -MAKEFLAGS -j1 > "$TMP/build.log" 2>&1 || { cat "$TMP/build.log"; exit 1; }
"$TMP/build/Vgrid_stream_fabric_tb" "+SPI_IMAGE=$TMP/spi.mem" "+UART_IMAGE=$TMP/uart.mem"
