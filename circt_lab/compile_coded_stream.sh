#!/usr/bin/env bash
# Source-checked USB DATA0 function, Ethernet stored-frame fragment or full
# Ethernet link session (ethernet_session).
set -euo pipefail
if [[ $# != 5 ]]; then echo "usage: $0 SOURCE usb\|ethernet\|ethernet_session PAYLOAD_HEX OUTPUT_PREFIX CIRCT_MAP" >&2; exit 2; fi
repo=$(cd "$(dirname "$0")/.." && pwd)
source_file=$(realpath "$1"); mode=$2; payload=$3; output=$(realpath -m "$4"); mapper=$(realpath "$5")
case "$mode" in
  usb) profile=usb_tx.json; nets=usb_coded_nets.json ;;
  ethernet) profile=ethernet_frame.json; nets=coded_stream_nets.json ;;
  ethernet_session) profile=ethernet_session.json; nets=ethernet_session_nets.json ;;
  *) echo 'mode must be usb, ethernet or ethernet_session' >&2; exit 2 ;;
esac
mkdir -p "$(dirname "$output")"
"${SPATIAL_FRONTEND:-$repo/target/debug/spatial}" frontend-mlir "$source_file" > "$output.frontend.mlir"
"$mapper" --lower-coded-stream "$output.frontend.mlir" "$repo/circt_lab/profiles/$profile" "$mode" "$payload" > "$output.program.json"
python3 "$repo/circt_lab/grid_route.py" "$repo/circt_lab/profiles/$nets" --emit-mlir > "$output.route.mlir"
"$mapper" --route-grid "$output.route.mlir" > "$output.routes.json"
python3 "$repo/circt_lab/grid_coded_image.py" "$output.routes.json" "$output.program.json" "$repo/../protocol-emulator/test/data/grid-spi.mem" "$output.mem"
python3 "$repo/circt_lab/show_grid_routes.py" "$output.routes.json" --program "$output.program.json" > "$output.map.txt"
