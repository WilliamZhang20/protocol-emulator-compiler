#!/usr/bin/env bash
# Restricted source test -> validated grid component + SRAM trace.
set -euo pipefail
if [[ $# != 5 ]]; then
  echo "usage: $0 SOURCE TEST_NAME OUTPUT_PREFIX CIRCT_MAP GRID_SPI_IMAGE" >&2; exit 2
fi
repo=$(cd "$(dirname "$0")/.." && pwd)
source_file=$(realpath "$1"); test_name=$2; output=$(realpath -m "$3")
mapper=$(realpath "$4"); base=$(realpath "$5")
mkdir -p "$(dirname "$output")"
"${SPATIAL_FRONTEND:-$repo/target/debug/spatial}" frontend-mlir "$source_file" > "$output.frontend.mlir"
"$mapper" --lower-jtag-test "$output.frontend.mlir" "$repo/circt_lab/profiles/jtag_scan.json" "$test_name" > "$output.program.json"
python3 "$repo/circt_lab/grid_jtag_component.py" --frontend "${SPATIAL_FRONTEND:-$repo/target/debug/spatial}" --mapper "$mapper" --base "$base" --output "$output.component.mem" "$source_file" > "$output.component.json"
python3 "$repo/circt_lab/grid_jtag_temporal_image.py" "$output.component.mem" "$output.program.json" "$output.mem"
