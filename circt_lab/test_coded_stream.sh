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
 cmp "$tmp/$mode.mem" "${RTL_ROOT:-$repo/../protocol-emulator}/test/data/grid-$mode-coded.mem"
 cmp "$tmp/$mode.program.json" "${RTL_ROOT:-$repo/../protocol-emulator}/test/data/grid-$mode-coded.json"
done
"$compile" "$repo/examples/usb.spt" usb 80c3 "$tmp/empty" "$mapper"
cmp "$tmp/empty.mem" "${RTL_ROOT:-$repo/../protocol-emulator}/test/data/grid-usb-empty.mem"
cmp "$tmp/empty.program.json" "${RTL_ROOT:-$repo/../protocol-emulator}/test/data/grid-usb-empty.json"
"$compile" "$repo/examples/usb.spt" usb 80c3ffffffffffffffff "$tmp/max" "$mapper"
cmp "$tmp/max.mem" "${RTL_ROOT:-$repo/../protocol-emulator}/test/data/grid-usb-max.mem"
cmp "$tmp/max.program.json" "${RTL_ROOT:-$repo/../protocol-emulator}/test/data/grid-usb-max.json"
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
python3 "$repo/circt_lab/generate_ethernet_mtu.py" "$repo/examples/ethernet_tx_coded.spt" "$tmp/mtu.spt"
"$compile" "$tmp/mtu.spt" ethernet '' "$tmp/mtu" "$mapper"
cmp "$tmp/mtu.mem" "${RTL_ROOT:-$repo/../protocol-emulator}/test/data/grid-ethernet-mtu.mem"
cmp "$tmp/mtu.program.json" "${RTL_ROOT:-$repo/../protocol-emulator}/test/data/grid-ethernet-mtu.json"
python3 "$repo/circt_lab/generate_ethernet_mtu.py" "$repo/examples/ethernet_tx_coded.spt" "$tmp/oversize.spt" --payload-bytes 1501
if "$compile" "$tmp/oversize.spt" ethernet '' "$tmp/reject" "$mapper" >"$tmp/error" 2>&1; then
 echo 'oversized Ethernet frame unexpectedly accepted' >&2; exit 1
fi
# Full 10BASE-T link session: the exported transmit() compiles onto the
# 5x3 grid; NLP_TICK is a source parameter (fast fixture for gate level).
"$compile" "$repo/examples/ethernet_tx_coded.spt" ethernet_session '' "$tmp/session" "$mapper"
cmp "$tmp/session.mem" "${RTL_ROOT:-$repo/../protocol-emulator}/test/data/grid-ethernet-session.mem"
cmp "$tmp/session.program.json" "${RTL_ROOT:-$repo/../protocol-emulator}/test/data/grid-ethernet-session.json"
python3 "$repo/circt_lab/generate_ethernet_session.py" "$repo/examples/ethernet_tx_coded.spt" "$tmp/fast.spt" --nlp-tick 4000
"$compile" "$tmp/fast.spt" ethernet_session '' "$tmp/fast" "$mapper"
cmp "$tmp/fast.mem" "${RTL_ROOT:-$repo/../protocol-emulator}/test/data/grid-ethernet-session-fast.mem"
cmp "$tmp/fast.program.json" "${RTL_ROOT:-$repo/../protocol-emulator}/test/data/grid-ethernet-session-fast.json"
python3 - "$repo/examples/ethernet_tx_coded.spt" "$tmp" <<'PY'
from pathlib import Path
import sys
src=Path(sys.argv[1]).read_text();out=Path(sys.argv[2])
body='    fire begin_frame\n    set_flags(sending)\n    bits.send(WIRE_FRAME)\n'
assert src.count(body)==1 and src.count('repeat 10')==3
# Rejected: a pulse while sending, unequal counts, a missing tail wait, and an
# extra declaration outside the profile.
(out/'s_pulse_sending.spt').write_text(src.replace(body,'    fire begin_frame\n    set_flags(sending)\n    fire link_pulse\n    wait pulse.fall\n    bits.send(WIRE_FRAME)\n'))
(out/'s_counts.spt').write_text(src.replace('repeat 10','repeat 9',1))
(out/'s_no_tail.spt').write_text(src.replace('    wait tail.tick\n','',1))
(out/'s_extra.spt').write_text(src.replace('flag sending','timer spare { every: 5 cycles, pulse, run: 1 }\nflag sending',1))
# Accepted: a shorter session (one pulse before the frame) compiles to fewer rows.
short=src.replace('    repeat 10 { wait next cadence.tick }\n    fire link_pulse\n    wait pulse.fall\n','',1)
assert short!=src;(out/'s_short.spt').write_text(short)
PY
for bad in s_pulse_sending s_counts s_no_tail s_extra; do
 if "$compile" "$tmp/$bad.spt" ethernet_session '' "$tmp/reject" "$mapper" >"$tmp/error" 2>&1; then
   echo "unsupported Ethernet session $bad accepted" >&2; exit 1
 fi
done
"$compile" "$tmp/s_short.spt" ethernet_session '' "$tmp/short" "$mapper"
python3 - "$tmp/session.program.json" "$tmp/short.program.json" <<'PY'
import json,sys
full,short=(json.load(open(p)) for p in sys.argv[1:])
assert full['sequencer_rows_used']==15 and short['sequencer_rows_used']==11, (full['sequencer_rows_used'],short['sequencer_rows_used'])
PY
"$compile" "$tmp/different_frame.spt" ethernet '' "$tmp/different" "$mapper"
python3 - "$tmp/ethernet.program.json" "$tmp/different.program.json" <<'PY'
import json,sys
x,y=(json.load(open(p)) for p in sys.argv[1:]);assert x['payload_bytes']!=y['payload_bytes'];assert x['base_words']==y['base_words']
PY
echo 'Coded source profiles, CIRCT routing, mutations and empty USB payload passed'
