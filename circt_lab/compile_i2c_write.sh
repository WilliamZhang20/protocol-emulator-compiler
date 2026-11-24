#!/usr/bin/env bash
# Restricted source profile -> Rust frontend -> CIRCT temporal lowering and
# router -> current-grid configuration image. Payload bytes go into SRAM.
set -euo pipefail
if [[ $# != 5 ]]; then
    echo "usage: $0 SOURCE ADDRESS PAYLOAD_HEX OUTPUT_PREFIX CIRCT_MAP" >&2
    exit 2
fi
repo=$(cd "$(dirname "$0")/.." && pwd)
source_file=$(realpath "$1")
address=$2
payload=$3
output=$(realpath -m "$4")
mapper=$(realpath "$5")
mkdir -p "$(dirname "$output")"
"$repo/target/debug/spatial" frontend-mlir "$source_file" > "$output.frontend.mlir"
"$mapper" --lower-i2c-write "$output.frontend.mlir" "$repo/circt_lab/profiles/i2c_write.json" "$address" "$payload" > "$output.program.json"
python3 "$repo/circt_lab/grid_route.py" "$repo/circt_lab/profiles/i2c_write_nets.json" --emit-mlir > "$output.route.mlir"
"$mapper" --route-grid "$output.route.mlir" > "$output.routes.json"
python3 "$repo/circt_lab/grid_temporal_image.py" "$output.routes.json" "$output.program.json" "$output.mem"
