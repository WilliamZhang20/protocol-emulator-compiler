#!/usr/bin/env bash
# Whole exported source function -> Rust frontend -> CIRCT temporal lowering
# and router -> 5x3 grid configuration image (spi.transfer, i2c.write_read).
set -euo pipefail
if [[ $# != 5 ]]; then echo "usage: $0 SOURCE FUNCTION EXAMPLE_HEX OUTPUT_PREFIX CIRCT_MAP" >&2; exit 2; fi
repo=$(cd "$(dirname "$0")/.." && pwd)
source_file=$(realpath "$1"); function=$2; example=$3; output=$(realpath -m "$4"); mapper=$(realpath "$5")
case "$function" in
  spi.transfer) profile=spi_transfer.json; nets=spi_transfer_nets.json ;;
  i2c.write_read) profile=i2c_write_read.json; nets=i2c_write_read_nets.json ;;
  *) echo 'function must be spi.transfer or i2c.write_read' >&2; exit 2 ;;
esac
mkdir -p "$(dirname "$output")"
"${SPATIAL_FRONTEND:-$repo/target/debug/spatial}" frontend-mlir "$source_file" > "$output.frontend.mlir"
"$mapper" --lower-temporal-function "$output.frontend.mlir" "$repo/circt_lab/profiles/$profile" "$function" "$example" > "$output.program.json"
python3 "$repo/circt_lab/grid_route.py" "$repo/circt_lab/profiles/$nets" --emit-mlir > "$output.route.mlir"
"$mapper" --route-grid "$output.route.mlir" > "$output.routes.json"
python3 "$repo/circt_lab/grid_coded_image.py" --page-rows "${PAGE_ROWS:-16}" "$output.routes.json" "$output.program.json" "${RTL_ROOT:-$repo/../protocol-emulator}/test/data/grid-spi.mem" "$output.mem"
python3 "$repo/circt_lab/show_grid_routes.py" "$output.routes.json" --program "$output.program.json" > "$output.map.txt"
# The source's own tests, for the generic chip runner (hardware repo test_compiled.py).
"${SPATIAL_FRONTEND:-$repo/target/debug/spatial}" test-plan "$source_file" > "$output.plan.json"
