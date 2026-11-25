#!/usr/bin/env bash
set -euo pipefail
if [[ $# != 1 ]]; then echo "usage: $0 CIRCT_MAP" >&2; exit 2; fi
repo=$(cd "$(dirname "$0")/.." && pwd); mapper=$(realpath "$1")
tmp=$(mktemp -d);trap 'rm -rf "$tmp"' EXIT
compile="$repo/circt_lab/compile_coded_stream.sh"
"$compile" "$repo/examples/usb.spt" usb 80c301ffff807e3f "$tmp/usb" "$mapper"
"$compile" "$repo/examples/ethernet_tx_coded.spt" ethernet '' "$tmp/ethernet" "$mapper"
for mode in usb ethernet; do
 grep -Fq "coded_shifter*" "$tmp/$mode.map.txt"
 cmp "$tmp/$mode.mem" "$repo/../protocol-emulator/test/data/grid-$mode-coded.mem"
 cmp "$tmp/$mode.program.json" "$repo/../protocol-emulator/test/data/grid-$mode-coded.json"
done
"$compile" "$repo/examples/usb.spt" usb 80c3 "$tmp/empty" "$mapper"
cmp "$tmp/empty.mem" "$repo/../protocol-emulator/test/data/grid-usb-empty.mem"
cmp "$tmp/empty.program.json" "$repo/../protocol-emulator/test/data/grid-usb-empty.json"
"$compile" "$repo/examples/usb.spt" usb 80c3ffffffffffffffff "$tmp/max" "$mapper"
cmp "$tmp/max.mem" "$repo/../protocol-emulator/test/data/grid-usb-max.mem"
cmp "$tmp/max.program.json" "$repo/../protocol-emulator/test/data/grid-usb-max.json"
if "$compile" "$repo/examples/usb.spt" usb 80c3000000000000000000 "$tmp/too_long" "$mapper" >"$tmp/error" 2>&1; then
 echo 'oversized low-speed USB packet unexpectedly accepted' >&2; exit 1
fi
python3 - "$repo/examples/usb.spt" "$repo/examples/ethernet_tx_coded.spt" "$tmp" <<'PY'
from pathlib import Path
import sys
usb=Path(sys.argv[1]).read_text();ethernet=Path(sys.argv[2]).read_text();out=Path(sys.argv[3])
assert 'limit: 6' in usb
(out/'bad_usb.spt').write_text(usb.replace('limit: 6','limit: 5',1))
assert 'every: 2 cycles' in ethernet
(out/'bad_ethernet.spt').write_text(ethernet.replace('every: 2 cycles','every: 3 cycles',1))
old='0x00, 0x01, 0x02, 0x03'; assert ethernet.count(old)==2
(out/'different_frame.spt').write_text(ethernet.replace(old,'0x00, 0x99, 0x02, 0x03'))
PY
for mode in usb ethernet; do
 if "$compile" "$tmp/bad_$mode.spt" "$mode" "$(test "$mode" = usb && printf 80c3)" "$tmp/reject" "$mapper" >"$tmp/error" 2>&1; then
   echo "unsupported $mode source accepted" >&2; exit 1
 fi
done
"$compile" "$tmp/different_frame.spt" ethernet '' "$tmp/different" "$mapper"
python3 - "$tmp/ethernet.program.json" "$tmp/different.program.json" <<'PY'
import json,sys
x,y=(json.load(open(p)) for p in sys.argv[1:]);assert x['payload_bytes']!=y['payload_bytes'];assert x['base_words']==y['base_words']
PY
echo 'Coded source profiles, CIRCT routing, mutations and empty USB payload passed'
