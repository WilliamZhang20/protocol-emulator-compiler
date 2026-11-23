#!/bin/sh
set -eu
: "${CIRCT_MAP:?Set CIRCT_MAP to the mapper executable or launcher}"
: "${CIRCT_OPT:?Set CIRCT_OPT to circt-opt executable or launcher}"
: "${SPATIAL_DIALECT:?Set SPATIAL_DIALECT to the built dialect plugin}"
HERE=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
SPATIAL_FRONTEND=${SPATIAL_FRONTEND:-"$HERE/../target/debug/spatial"}
TEST_TMP=$(mktemp -d)
trap 'rm -rf "$TEST_TMP"' EXIT HUP INT TERM
"$CIRCT_OPT" "$HERE/control_fsm.mlir" -o /dev/null
"$SPATIAL_FRONTEND" frontend-mlir "$HERE/../examples/spi.spt" > "$TEST_TMP/frontend.mlir"
"$CIRCT_OPT" "--load-dialect-plugin=$SPATIAL_DIALECT" "$TEST_TMP/frontend.mlir" -o "$TEST_TMP/frontend-checked.mlir"
"$CIRCT_MAP" --verify "$TEST_TMP/frontend.mlir" > /dev/null
# The memory-backed slice uses protocol rows from an explicit source state
# machine, rather than the separate hand-authored SPI control FSM.
"$SPATIAL_FRONTEND" frontend-mlir "$HERE/examples/memory_spi.spt" > "$TEST_TMP/memory-source.mlir"
"$CIRCT_OPT" "--load-dialect-plugin=$SPATIAL_DIALECT" "$TEST_TMP/memory-source.mlir" -o /dev/null
"$CIRCT_MAP" --compile-memory-spi "$TEST_TMP/memory-source.mlir" 2652 "$TEST_TMP/memory-source.mem" > "$TEST_TMP/memory-source.json"
"$CIRCT_MAP" --memory-spi-graph "$TEST_TMP/memory-source.mlir" > "$TEST_TMP/memory-graph.mlir"
"$CIRCT_OPT" "--load-dialect-plugin=$SPATIAL_DIALECT" "$TEST_TMP/memory-graph.mlir" -o /dev/null
"$CIRCT_OPT" "--load-dialect-plugin=$SPATIAL_DIALECT" "$HERE/candidates/memory-spi-graph.mlir" -o /dev/null
"$CIRCT_OPT" "--load-dialect-plugin=$SPATIAL_DIALECT" "$HERE/candidates/uart-capability-hypothesis.mlir" -o /dev/null
RTL_ROOT=${RTL_ROOT:-"$HERE/../../protocol-emulator"}
if [ -f "$RTL_ROOT/src/memory_spi_fabric.v" ]; then python3 "$HERE/test_rtl_inventory.py" "$RTL_ROOT"; fi
"$SPATIAL_FRONTEND" frontend-mlir "$HERE/../examples/uart.spt" > "$TEST_TMP/uart-source.mlir"
"$CIRCT_MAP" --uart-graph "$TEST_TMP/uart-source.mlir" > "$TEST_TMP/uart-graph.mlir"
"$CIRCT_OPT" "--load-dialect-plugin=$SPATIAL_DIALECT" "$TEST_TMP/uart-graph.mlir" -o /dev/null
"$CIRCT_MAP" --compile-v2-spi "$TEST_TMP/memory-source.mlir" "$TEST_TMP/v2-spi.mem" > "$TEST_TMP/v2-spi.json"
"$CIRCT_MAP" --compile-v2-uart "$TEST_TMP/uart-source.mlir" "$TEST_TMP/v2-uart.mem" > "$TEST_TMP/v2-uart.json"
"$CIRCT_MAP" --compile-v2-uart-stream "$TEST_TMP/uart-source.mlir" "$TEST_TMP/v2-uart-stream.mem" > "$TEST_TMP/v2-uart-stream.json"
"$CIRCT_MAP" --compile-v2-unified-spi "$TEST_TMP/memory-source.mlir" "$TEST_TMP/v2-unified-spi.mem" > "$TEST_TMP/v2-unified-spi.json"
"$CIRCT_MAP" --compile-v2-unified-uart "$TEST_TMP/uart-source.mlir" "$TEST_TMP/v2-unified-uart.mem" > "$TEST_TMP/v2-unified-uart.json"
python3 - "$TEST_TMP" <<'PY'
import json, pathlib, sys
p=pathlib.Path(sys.argv[1]); spi=[int(x,16) for x in (p/'v2-spi.mem').read_text().splitlines()]
uart=[int(x,16) for x in (p/'v2-uart.mem').read_text().splitlines()]
assert len(spi)==len(uart)==26 and spi[:6]==[8,8,1,1,44,1]
assert uart[:6]==[62,64,31,64,10,10] and uart[22:]==[0x235,0x200,0,1]
for name in ('v2-spi.json','v2-uart.json'):
 r=json.loads((p/name).read_text());assert r['source_validated'] and not r['full_source_program_compiled'] and not r['general_mapping']
stream=json.loads((p/'v2-uart-stream.json').read_text())
assert stream['target']=='fabric-v2-uart-stream-experiment' and not stream['full_source_program_compiled']
assert (p/'v2-uart-stream.mem').read_bytes()==(p/'v2-uart.mem').read_bytes()
for name,mode in [('spi',0),('uart',1)]:
 image=[int(x,16) for x in (p/f'v2-unified-{name}.mem').read_text().splitlines()]
 report=json.loads((p/f'v2-unified-{name}.json').read_text())
 assert len(image)==27 and image[26]==mode and report['target']=='fabric-v2-unified-experiment' and not report['full_source_program_compiled']
PY
if [ -n "${V2_UNIFIED_SPI_FIXTURE:-}" ]; then cmp "$TEST_TMP/v2-unified-spi.mem" "$V2_UNIFIED_SPI_FIXTURE"; fi
if [ -n "${V2_UNIFIED_UART_FIXTURE:-}" ]; then cmp "$TEST_TMP/v2-unified-uart.mem" "$V2_UNIFIED_UART_FIXTURE"; fi
if [ -n "${V2_UNIFIED_IMAGE_TB:-}" ]; then
  "$V2_UNIFIED_IMAGE_TB" "+SPI_IMAGE=$TEST_TMP/v2-unified-spi.mem" "+UART_IMAGE=$TEST_TMP/v2-unified-uart.mem"
fi
if [ -n "${V2_MEMORY_IMAGE_TB:-}" ]; then
  "$V2_MEMORY_IMAGE_TB" "+SPI_IMAGE=$TEST_TMP/v2-spi.mem" "+UART_IMAGE=$TEST_TMP/v2-uart.mem"
fi
if [ -n "${V2_UART_STREAM_IMAGE_TB:-}" ]; then
  "$V2_UART_STREAM_IMAGE_TB" "+UART_IMAGE=$TEST_TMP/v2-uart-stream.mem"
fi
sed 's/data(8)/data(7)/g' "$HERE/../examples/uart.spt" > "$TEST_TMP/uart-unsupported.spt"
"$SPATIAL_FRONTEND" frontend-mlir "$TEST_TMP/uart-unsupported.spt" > "$TEST_TMP/uart-unsupported.mlir"
if "$CIRCT_MAP" --compile-v2-uart "$TEST_TMP/uart-unsupported.mlir" "$TEST_TMP/v2-unsupported.mem" > /dev/null 2> "$TEST_TMP/error"; then
  echo 'v2 accepted unsupported UART frame' >&2; exit 1
fi
test ! -e "$TEST_TMP/v2-unsupported.mem"
if "$CIRCT_MAP" --compile-v2-uart-stream "$TEST_TMP/uart-unsupported.mlir" "$TEST_TMP/v2-stream-unsupported.mem" > /dev/null 2> "$TEST_TMP/error"; then
  echo 'v2 stream accepted unsupported UART frame' >&2; exit 1
fi
test ! -e "$TEST_TMP/v2-stream-unsupported.mem"
sed 's/const BIT = 64 cycles/const BIT = 80 cycles/' "$HERE/../examples/uart.spt" > "$TEST_TMP/uart-80.spt"
"$SPATIAL_FRONTEND" frontend-mlir "$TEST_TMP/uart-80.spt" > "$TEST_TMP/uart-80.mlir"
"$CIRCT_MAP" --compile-v2-uart "$TEST_TMP/uart-80.mlir" "$TEST_TMP/v2-uart-80.mem" > /dev/null
"$CIRCT_MAP" --compile-v2-uart-stream "$TEST_TMP/uart-80.mlir" "$TEST_TMP/v2-uart-stream-80.mem" > /dev/null
"$CIRCT_MAP" --compile-v2-unified-uart "$TEST_TMP/uart-80.mlir" "$TEST_TMP/v2-unified-uart-80.mem" > /dev/null
sed 's/data(12, msb_first)/data(9, msb_first)/' "$HERE/examples/memory_spi.spt" > "$TEST_TMP/spi-9.spt"
"$SPATIAL_FRONTEND" frontend-mlir "$TEST_TMP/spi-9.spt" > "$TEST_TMP/spi-9.mlir"
"$CIRCT_MAP" --compile-v2-spi "$TEST_TMP/spi-9.mlir" "$TEST_TMP/v2-spi-9.mem" > /dev/null
python3 - "$TEST_TMP" <<'PY'
import pathlib, sys
p=pathlib.Path(sys.argv[1]); uart=[int(x,16) for x in (p/'v2-uart-80.mem').read_text().splitlines()]
spi=[int(x,16) for x in (p/'v2-spi-9.mem').read_text().splitlines()]
assert uart[:4]==[78,80,39,80] and spi[4]==41
PY
if [ -n "${V2_MEMORY_IMAGE_TB:-}" ]; then
  "$V2_MEMORY_IMAGE_TB" "+SPI_IMAGE=$TEST_TMP/v2-spi.mem" "+UART_IMAGE=$TEST_TMP/v2-uart-80.mem"
fi
if [ -n "${V2_UART_STREAM_IMAGE_TB:-}" ]; then
  "$V2_UART_STREAM_IMAGE_TB" "+UART_IMAGE=$TEST_TMP/v2-uart-stream-80.mem"
fi
if [ -n "${V2_UNIFIED_IMAGE_TB:-}" ]; then
  "$V2_UNIFIED_IMAGE_TB" "+SPI_IMAGE=$TEST_TMP/v2-unified-spi.mem" "+UART_IMAGE=$TEST_TMP/v2-unified-uart-80.mem"
fi
if [ -n "${MEMORY_SPI_FIXTURE:-}" ]; then
  python3 "$HERE/test_graphs.py" --mapper "$CIRCT_MAP" --frontend "$SPATIAL_FRONTEND" --fixture "$MEMORY_SPI_FIXTURE"
else
  python3 "$HERE/test_graphs.py" --mapper "$CIRCT_MAP" --frontend "$SPATIAL_FRONTEND"
fi
python3 - "$TEST_TMP/memory-source.mem" "$TEST_TMP/memory-source.json" <<'PY'
import json, sys
image = [int(x, 16) for x in open(sys.argv[1])]
report = json.load(open(sys.argv[2]))
assert len(image) == 19 and image[0] == 0x0c08 and image[1] == 0x8000 and image[18] == 0xa5c0
assert report['protocol_control_from_source'] and not report['full_program_compiled']
PY
if [ -n "${MEMORY_SPI_FIXTURE:-}" ]; then cmp "$TEST_TMP/memory-source.mem" "$MEMORY_SPI_FIXTURE"; fi
if [ -n "${MEMORY_SPI_IMAGE_TB:-}" ]; then "$MEMORY_SPI_IMAGE_TB" "+IMAGE=$TEST_TMP/memory-source.mem"; fi
sed 's/start: transfer_control.action2/start: transfer_control.action1/' "$HERE/examples/memory_spi.spt" > "$TEST_TMP/memory-bad.spt"
"$SPATIAL_FRONTEND" frontend-mlir "$TEST_TMP/memory-bad.spt" > "$TEST_TMP/memory-bad.mlir"
if "$CIRCT_MAP" --compile-memory-spi "$TEST_TMP/memory-bad.mlir" 2652 "$TEST_TMP/memory-bad.mem" > /dev/null 2> "$TEST_TMP/error"; then
  echo 'accepted unsupported shifter start binding' >&2; exit 1
fi
if "$CIRCT_MAP" --compile-v2-spi "$TEST_TMP/memory-bad.mlir" "$TEST_TMP/v2-bad.mem" > /dev/null 2> "$TEST_TMP/error"; then
  echo 'v2 accepted unsupported shifter start binding' >&2; exit 1
fi
test ! -e "$TEST_TMP/v2-bad.mem"
test ! -e "$TEST_TMP/memory-bad.mem"
sed 's/sample: miso at/sample: trigger at/' "$HERE/examples/memory_spi.spt" > "$TEST_TMP/memory-wrong-input.spt"
"$SPATIAL_FRONTEND" frontend-mlir "$TEST_TMP/memory-wrong-input.spt" > "$TEST_TMP/memory-wrong-input.mlir"
if "$CIRCT_MAP" --compile-memory-spi "$TEST_TMP/memory-wrong-input.mlir" 2652 "$TEST_TMP/memory-wrong-input.mem" > /dev/null 2> "$TEST_TMP/error"; then
  echo 'accepted an input pin the prototype does not connect' >&2; exit 1
fi
test ! -e "$TEST_TMP/memory-wrong-input.mem"
sed 's/-> 1 action 2/-> 1 action 1/g' "$HERE/examples/memory_spi.spt" > "$TEST_TMP/memory-changed.spt"
"$SPATIAL_FRONTEND" frontend-mlir "$TEST_TMP/memory-changed.spt" > "$TEST_TMP/memory-changed.mlir"
if "$CIRCT_MAP" --compile-v2-spi "$TEST_TMP/memory-changed.mlir" "$TEST_TMP/v2-changed.mem" > /dev/null 2> "$TEST_TMP/error"; then
  echo 'v2 accepted unsupported SPI control semantics' >&2; exit 1
fi
test ! -e "$TEST_TMP/v2-changed.mem"
"$CIRCT_MAP" --compile-memory-spi "$TEST_TMP/memory-changed.mlir" 2652 "$TEST_TMP/memory-changed.mem" > /dev/null
python3 - "$TEST_TMP/memory-source.mem" "$TEST_TMP/memory-changed.mem" <<'PY'
import sys
a, b = ([int(x, 16) for x in open(p)] for p in sys.argv[1:])
assert a[:2] == b[:2] and a[18] == b[18]
assert a[2:18] != b[2:18], 'source control changes must change the control table'
PY
if [ -n "${MEMORY_SPI_IMAGE_TB:-}" ]; then
  if (ulimit -c 0; "$MEMORY_SPI_IMAGE_TB" "+IMAGE=$TEST_TMP/memory-changed.mem") > "$TEST_TMP/memory-changed.log" 2>&1; then
    echo 'mutated source control unexpectedly produced the reference transfer' >&2; exit 1
  fi
  grep -Fq 'transfer samples=' "$TEST_TMP/memory-changed.log"
fi
# Extract only the timer/shifter component from a real SPI source. The hand-
# authored transaction controller and supplied payload remain separate.
"$CIRCT_MAP" --derive-spi-component "$TEST_TMP/frontend.mlir" sck_clock bits 165 > "$TEST_TMP/derived-spi.mlir" 2> "$TEST_TMP/derived-warning"
grep -Fq 'full-program semantics are not mapped' "$TEST_TMP/derived-warning"
"$CIRCT_OPT" "--load-dialect-plugin=$SPATIAL_DIALECT" "$TEST_TMP/derived-spi.mlir" -o /dev/null
"$CIRCT_MAP" "$HERE/prototype.mlir" "$TEST_TMP/derived-spi.mlir" "$HERE/control_fsm.mlir" "$TEST_TMP/derived-spi.mem" > /dev/null
python3 - "$TEST_TMP/derived-spi.mem" <<'PY'
import sys
image = [int(x, 16) for x in open(sys.argv[1])]
assert image[0] == 0x0808 and image[18] == 0xa500
PY
if [ -n "${SPI_IMAGE_TB:-}" ]; then
  "$SPI_IMAGE_TB" "+IMAGE=$TEST_TMP/derived-spi.mem"
fi
python3 - "$HERE/../examples/spi.spt" "$TEST_TMP" <<'PY'
from pathlib import Path
import sys
source = Path(sys.argv[1]).read_text()
out = Path(sys.argv[2])
for name, old, new in [
    ('width12', 'data(8, msb_first)', 'data(12, msb_first)'),
    ('period10', 'const HALF = 8 cycles', 'const HALF = 10 cycles'),
    ('lsb', 'data(8, msb_first)', 'data(8, lsb_first)'),
    ('edge', 'shift: sck_clock.fall', 'shift: sck_clock.rise'),
    ('fast', 'const HALF = 8 cycles', 'const HALF = 7 cycles'),
]:
    assert old in source
    (out / (name + '.spt')).write_text(source.replace(old, new, 1))
PY
for variant in width12 period10 lsb edge fast; do
  "$SPATIAL_FRONTEND" frontend-mlir "$TEST_TMP/$variant.spt" > "$TEST_TMP/$variant.mlir"
done
for variant in width12 period10; do
  "$CIRCT_MAP" --derive-spi-component "$TEST_TMP/$variant.mlir" sck_clock bits 165 > "$TEST_TMP/$variant-spi.mlir"
  "$CIRCT_MAP" "$HERE/prototype.mlir" "$TEST_TMP/$variant-spi.mlir" "$HERE/control_fsm.mlir" "$TEST_TMP/$variant.mem" > /dev/null
  if [ -n "${SPI_IMAGE_TB:-}" ]; then "$SPI_IMAGE_TB" "+IMAGE=$TEST_TMP/$variant.mem"; fi
done
python3 - "$TEST_TMP/width12.mem" "$TEST_TMP/period10.mem" <<'PY'
import sys
assert int(open(sys.argv[1]).readline(), 16) == 0x0c08
assert int(open(sys.argv[2]).readline(), 16) == 0x080a
PY
for variant in lsb edge fast; do
  if "$CIRCT_MAP" --derive-spi-component "$TEST_TMP/$variant.mlir" sck_clock bits 165 > /dev/null 2> "$TEST_TMP/error"; then
    echo "unsupported $variant component was accepted" >&2; exit 1
  fi
done
test "$(grep -c 'spatial.frontend_item' "$TEST_TMP/frontend-checked.mlir")" -eq 19
test "$(grep -c 'spatial.frontend_input' "$TEST_TMP/frontend-checked.mlir")" -gt 0
sed '0,/kind = "clock"/s/kind = "clock"/kind = "not_clock"/' "$TEST_TMP/frontend.mlir" > "$TEST_TMP/frontend-bad.mlir"
if "$CIRCT_OPT" "--load-dialect-plugin=$SPATIAL_DIALECT" "$TEST_TMP/frontend-bad.mlir" -o /dev/null 2> "$TEST_TMP/error"; then
  echo 'registered verifier accepted a mismatched frontend item kind' >&2; exit 1
fi
grep -Fq 'item_json kind must match' "$TEST_TMP/error"
# A real .spt protocol graph now lowers through the registered typed operation
# to the prototype control table. This is not a full-program image.
"$SPATIAL_FRONTEND" frontend-mlir "$HERE/examples/control.spt" > "$TEST_TMP/control-frontend.mlir"
"$CIRCT_OPT" "--load-dialect-plugin=$SPATIAL_DIALECT" "$TEST_TMP/control-frontend.mlir" -o /dev/null
"$CIRCT_MAP" --control-rows "$TEST_TMP/control-frontend.mlir" handshake "$TEST_TMP/control.mem" > "$TEST_TMP/control-report.json"
python3 - "$TEST_TMP/control.mem" "$TEST_TMP/control-report.json" <<'PY'
import json, sys
rows = [int(x, 16) for x in open(sys.argv[1])]
expected = []
for state in range(4):
    for a in range(2):
        for _b in range(2):
            expected.append(5 if state == 0 and a else 8 if state == 1 and not a else state)
assert rows == expected, (rows, expected)
r = json.load(open(sys.argv[2]))
assert r['control_table_written'] and not r['input_binding_verified'] and not r['full_program_compiled']
PY
if [ -n "${CONTROL_IMAGE_TB:-}" ]; then
  "$CONTROL_IMAGE_TB" "+IMAGE=$TEST_TMP/control.mem"
else
  echo 'RTL control-image test skipped: CONTROL_IMAGE_TB not set'
fi
"$SPATIAL_FRONTEND" frontend-mlir "$HERE/examples/control_full.spt" > "$TEST_TMP/full.mlir"
"$CIRCT_MAP" --compile-control "$TEST_TMP/full.mlir" "$TEST_TMP/full.mem" > "$TEST_TMP/full.json"
python3 - "$TEST_TMP/full.mem" "$TEST_TMP/full.json" <<'PY'
import json, sys
rows = [int(x, 16) for x in open(sys.argv[1])]
report = json.load(open(sys.argv[2]))
assert len(rows) == 17 and rows[-1] == 3
assert report['full_program_compiled'] and report['input_binding_verified']
assert report['pin_bindings'] == {'request': 'input0', 'high_out': 'output0', 'low_out': 'output1'}
PY
if [ -n "${FULL_CONTROL_IMAGE_TB:-}" ]; then
  "$FULL_CONTROL_IMAGE_TB" "+IMAGE=$TEST_TMP/full.mem"
else
  echo 'RTL complete control-fabric test skipped: FULL_CONTROL_IMAGE_TB not set'
fi
if "$CIRCT_MAP" --compile-control "$TEST_TMP/frontend.mlir" "$TEST_TMP/unsupported.mem" > /dev/null 2> "$TEST_TMP/error"; then
  echo 'SPI must be rejected by the restricted control target' >&2; exit 1
fi
test ! -e "$TEST_TMP/unsupported.mem"
sed 's/states = 2 : i64/states = 3 : i64/' "$TEST_TMP/control-frontend.mlir" > "$TEST_TMP/control-tampered.mlir"
if "$CIRCT_OPT" "--load-dialect-plugin=$SPATIAL_DIALECT" "$TEST_TMP/control-tampered.mlir" -o /dev/null 2> "$TEST_TMP/error"; then
  echo 'typed protocol disagreed with frontend but verified' >&2; exit 1
fi
grep -Fq 'typed protocol does not match' "$TEST_TMP/error"
# Check a second real frontend workload with different resource types.
"$SPATIAL_FRONTEND" frontend-mlir "$HERE/../examples/i2c.spt" > "$TEST_TMP/frontend-i2c.mlir"
"$CIRCT_OPT" "--load-dialect-plugin=$SPATIAL_DIALECT" "$TEST_TMP/frontend-i2c.mlir" -o /dev/null
"$SPATIAL_FRONTEND" frontend-mlir "$HERE/../examples/uart.spt" > "$TEST_TMP/frontend-uart.mlir"
"$CIRCT_OPT" "--load-dialect-plugin=$SPATIAL_DIALECT" "$TEST_TMP/frontend-uart.mlir" -o /dev/null
test "$(grep -c 'spatial.frontend_stream' "$TEST_TMP/frontend-uart.mlir")" -eq 2
"$SPATIAL_FRONTEND" model-ir "$HERE/../examples/uart.spt" > "$TEST_TMP/frontend-original.json"
"$CIRCT_MAP" --frontend-json "$TEST_TMP/frontend-uart.mlir" > "$TEST_TMP/frontend-roundtrip.json"
python3 - "$TEST_TMP/frontend-original.json" "$TEST_TMP/frontend-roundtrip.json" <<'PY'
import json, sys
original = json.load(open(sys.argv[1]))
original.pop('requirements')  # Derived on demand, not part of LogicalProgram.
assert original == json.load(open(sys.argv[2])), 'frontend IR changed on MLIR roundtrip'
PY
# The existing lexer accepts UTF-8 and backslashes inside string literals.
# Check MLIR byte escaping and JSON roundtrip, not just ASCII resource names.
python3 - "$TEST_TMP/unicode.spt" <<'PY'
from pathlib import Path
import sys
Path(sys.argv[1]).write_text('clock 50 MHz\ntest "café \\ path" {}\n', encoding='utf-8')
PY
"$SPATIAL_FRONTEND" frontend-mlir "$TEST_TMP/unicode.spt" > "$TEST_TMP/unicode.mlir"
"$CIRCT_OPT" "--load-dialect-plugin=$SPATIAL_DIALECT" "$TEST_TMP/unicode.mlir" -o /dev/null
"$SPATIAL_FRONTEND" model-ir "$TEST_TMP/unicode.spt" > "$TEST_TMP/unicode-original.json"
"$CIRCT_MAP" --frontend-json "$TEST_TMP/unicode.mlir" > "$TEST_TMP/unicode-roundtrip.json"
python3 - "$TEST_TMP/unicode-original.json" "$TEST_TMP/unicode-roundtrip.json" <<'PY'
import json, sys
original = json.load(open(sys.argv[1]))
original.pop('requirements')
assert original == json.load(open(sys.argv[2])), 'UTF-8 or backslash changed on MLIR roundtrip'
PY
# Route real frontend-derived Boolean dependencies against a separately
# declared hypothetical graph. Stream/word connections are not yet routed.
"$SPATIAL_FRONTEND" frontend-mlir "$HERE/../examples/loopback.spt" > "$TEST_TMP/loopback.mlir"
"$CIRCT_MAP" --analyze "$HERE/candidates/loopback-boolean.mlir" "$TEST_TMP/loopback.mlir" > "$TEST_TMP/loopback.json"
python3 - "$TEST_TMP/loopback.json" <<'PY'
import json, sys
r = json.load(open(sys.argv[1]))
assert r['frontend_dependency_only'] and r['unmodeled_word_streams'] == 2
assert not r['hardware_fidelity_verified'] and not r['image_emitted']
assert len(r['assignments']) == 10
assert next(x for x in r['assignments'] if x['sink'] == 'out_wire.pin.drive')['selector_hops'] == 2
PY
# Compound expressions from the real SPI frontend must fail explicitly;
# routing their references separately would silently discard logic semantics.
if "$CIRCT_MAP" --analyze "$HERE/candidates/loopback-boolean.mlir" "$TEST_TMP/frontend.mlir" > /dev/null 2> "$TEST_TMP/error"; then
  echo 'frontend compound logic should not be accepted by the simple adapter' >&2; exit 1
fi
grep -Fq 'only simple single-signal frontend inputs' "$TEST_TMP/error"
"$CIRCT_MAP" "$HERE/prototype.mlir" "$HERE/spi.mlir" "$HERE/control_fsm.mlir" "$TEST_TMP/image.mem" > "$TEST_TMP/mapped.mlir"
"$CIRCT_OPT" "--load-dialect-plugin=$SPATIAL_DIALECT" "$TEST_TMP/mapped.mlir" -o /dev/null
cat > "$TEST_TMP/expected.mem" <<'DATA'
0808
0000
0000
0000
0005
0005
000e
000e
000e
000e
0006
0013
0006
0013
0013
0013
0005
0005
a500
DATA
cmp "$TEST_TMP/expected.mem" "$TEST_TMP/image.mem"
# Multiple typed widths exercise real lowering and optionally the same RTL
# peer; absence of the optional simulator is reported, not treated as a pass.
for spec in '1 1' '5 21' '8 165' '12 2652' '16 42435'; do
  set -- $spec
  width=$1
  value=$2
  sed -e "s/width = 8 : i64/width = $width : i64/" \
      -e "s/tx_value = 165 : i64/tx_value = $value : i64/" \
      -e "s/() -> i8/() -> i$width/" "$HERE/spi.mlir" > "$TEST_TMP/width.mlir"
  "$CIRCT_MAP" "$HERE/prototype.mlir" "$TEST_TMP/width.mlir" "$HERE/control_fsm.mlir" "$TEST_TMP/width.mem" > "$TEST_TMP/width-out.mlir"
  "$CIRCT_OPT" "--load-dialect-plugin=$SPATIAL_DIALECT" "$TEST_TMP/width-out.mlir" -o /dev/null
  if [ -n "${SPI_IMAGE_TB:-}" ]; then "$SPI_IMAGE_TB" "+IMAGE=$TEST_TMP/width.mem"; fi
done
if [ -z "${SPI_IMAGE_TB:-}" ]; then echo 'RTL image tests skipped: SPI_IMAGE_TB not set'; fi
expect_fail() {
  if "$CIRCT_MAP" "$1" "$2" "$HERE/control_fsm.mlir" "$TEST_TMP/rejected.mem" > /dev/null 2> "$TEST_TMP/error"; then
    echo "expected mapping to fail: $3" >&2
    exit 1
  fi
  if ! grep -Fq "$3" "$TEST_TMP/error"; then
    echo "unexpected error instead of: $3" >&2
    cat "$TEST_TMP/error" >&2
    exit 1
  fi
  if [ -e "$TEST_TMP/rejected.mem" ]; then echo "failed mapping wrote an image" >&2; exit 1; fi
}
sed -e 's/width = 8 : i64/width = 17 : i64/' -e 's/() -> i8/() -> i17/' "$HERE/spi.mlir" > "$TEST_TMP/wide.mlir"
expect_fail "$HERE/prototype.mlir" "$TEST_TMP/wide.mlir" 'invalid width'
sed 's/() -> i8/() -> i16/' "$HERE/spi.mlir" > "$TEST_TMP/type.mlir"
expect_fail "$HERE/prototype.mlir" "$TEST_TMP/type.mlir" 'result type must be a signless integer'
sed 's/storage_words = 4 : i64/storage_words = 3 : i64/' "$HERE/prototype.mlir" > "$TEST_TMP/storage.mlir"
expect_fail "$TEST_TMP/storage.mlir" "$HERE/spi.mlir" 'architecture capacities'
sed 's/pad_max_output_hz_estimate = 66000000/pad_max_output_hz_estimate = 1000000/' "$HERE/prototype.mlir" > "$TEST_TMP/pad.mlir"
expect_fail "$TEST_TMP/pad.mlir" "$HERE/spi.mlir" 'requested clock exceeds'
sed 's/mosi = "shift.out"/mosi = "timer.level"/' "$HERE/spi.mlir" > "$TEST_TMP/route.mlir"
expect_fail "$HERE/prototype.mlir" "$TEST_TMP/route.mlir" 'only supports'
sed 's/fsm.return %done/fsm.return %trigger/' "$HERE/control_fsm.mlir" > "$TEST_TMP/wrong-fsm.mlir"
if "$CIRCT_MAP" "$HERE/prototype.mlir" "$HERE/spi.mlir" "$TEST_TMP/wrong-fsm.mlir" "$TEST_TMP/rejected.mem" > /dev/null 2> "$TEST_TMP/error"; then
  echo 'expected incorrect CIRCT FSM to fail' >&2
  exit 1
fi
grep -Fq 'FSM guard does not match' "$TEST_TMP/error"
# Actual dialect verification must reject invalid operations without the mapper.
if "$CIRCT_OPT" "--load-dialect-plugin=$SPATIAL_DIALECT" "$TEST_TMP/type.mlir" -o /dev/null 2> "$TEST_TMP/error"; then
  echo 'registered verifier accepted a mismatched result type' >&2; exit 1
fi
grep -Fq 'result type must be a signless integer' "$TEST_TMP/error"
printf 'module { "spatial.unknown"() : () -> () }\n' > "$TEST_TMP/unknown.mlir"
if "$CIRCT_OPT" "--load-dialect-plugin=$SPATIAL_DIALECT" "$TEST_TMP/unknown.mlir" -o /dev/null 2> "$TEST_TMP/error"; then
  echo 'registered dialect unexpectedly accepted an unknown operation' >&2; exit 1
fi
# A hypothetical selector-order change is analyzable, but must not emit an
# image claiming to match unchanged physical RTL.
sed 's/sources = \["shift.out", "timer.level", "zero", "one"\]/sources = ["timer.level", "shift.out", "zero", "one"]/' "$HERE/prototype.mlir" > "$TEST_TMP/alternate.mlir"
"$CIRCT_MAP" --analyze "$TEST_TMP/alternate.mlir" "$HERE/spi.mlir" > "$TEST_TMP/analysis.json"
python3 - "$TEST_TMP/analysis.json" <<'PY'
import json, sys
report = json.load(open(sys.argv[1]))
mosi = next(x for x in report['assignments'] if x['sink'] == 'mosi')
assert mosi['choice'] == 1 and not report['hardware_fidelity_verified'] and not report['image_emitted']
PY
expect_fail "$TEST_TMP/alternate.mlir" "$HERE/spi.mlir" 'route source ordering does not match'
# A candidate with different declared capacities is analyzable; it is not
# permitted to emit a bitstream for the fixed hardware ABI.
sed -e 's/storage_words = 4 : i64/storage_words = 8 : i64/' -e 's/shift_width = 16 : i64/shift_width = 32 : i64/' "$HERE/prototype.mlir" > "$TEST_TMP/larger.mlir"
"$CIRCT_MAP" --analyze "$TEST_TMP/larger.mlir" "$HERE/spi.mlir" > "$TEST_TMP/larger.json"
expect_fail "$TEST_TMP/larger.mlir" "$HERE/spi.mlir" 'architecture capacities'
# Logical widths are not capped to today's 16-bit target. The 128-bit
# hypothesis is model-only; image emission remains restricted to real RTL.
sed -e 's/shift_width = 16 : i64/shift_width = 128 : i64/' -e 's/storage_word_bits = 16 : i64/storage_word_bits = 128 : i64/' "$HERE/prototype.mlir" > "$TEST_TMP/wide-arch.mlir"
sed -e 's/width = 8 : i64/width = 128 : i64/' -e 's/() -> i8/() -> i128/' "$HERE/spi.mlir" > "$TEST_TMP/wide-program.mlir"
"$CIRCT_MAP" --analyze "$TEST_TMP/wide-arch.mlir" "$TEST_TMP/wide-program.mlir" > "$TEST_TMP/wide-analysis.json"
expect_fail "$TEST_TMP/wide-arch.mlir" "$TEST_TMP/wide-program.mlir" 'architecture capacities'
# Two destinations compete for one relay. A successful mapping must undo its
# first route choice and reserve that relay for the destination that needs it.
"$CIRCT_MAP" --analyze "$HERE/candidates/backtrack.mlir" "$HERE/candidates/backtrack-program.mlir" > "$TEST_TMP/backtrack.json"
python3 - "$TEST_TMP/backtrack.json" <<'PY'
import json, sys
r = json.load(open(sys.argv[1]))
routes = {x['sink']: x['source'] for x in r['selected_routes']}
assert routes['a'] == 'route.y' and routes['b'] == 'route.x'
assert routes['x'] == 'producer_b' and routes['y'] == 'producer_a'
assert all(x['selector_hops'] == 2 and x['selector_cycles'] == 2 for x in r['assignments'])
PY
sed 's/"route.x", "route.y"/"route.x"/' "$HERE/candidates/backtrack.mlir" > "$TEST_TMP/conflict.mlir"
if "$CIRCT_MAP" --analyze "$TEST_TMP/conflict.mlir" "$HERE/candidates/backtrack-program.mlir" > /dev/null 2> "$TEST_TMP/error"; then
  echo 'expected shared-selector conflict to fail' >&2; exit 1
fi
grep -Fq 'no compatible selector assignment' "$TEST_TMP/error"
sed 's/route.y/route.missing/' "$HERE/candidates/backtrack.mlir" > "$TEST_TMP/missing-relay.mlir"
if "$CIRCT_OPT" "--load-dialect-plugin=$SPATIAL_DIALECT" "$TEST_TMP/missing-relay.mlir" -o /dev/null 2> "$TEST_TMP/error"; then
  echo 'registered verifier accepted a missing relay' >&2; exit 1
fi
grep -Fq 'route references missing relay' "$TEST_TMP/error"
python3 "$HERE/test_routes.py" --mapper "$CIRCT_MAP" --cases 30
python3 "$HERE/test_control.py" --frontend "$SPATIAL_FRONTEND" --mapper "$CIRCT_MAP" --cases 40
python3 "$HERE/test_workload_evidence.py" --frontend "$SPATIAL_FRONTEND" --mapper "$CIRCT_MAP"
python3 "$HERE/investigations/route_timing_probe.py" --frontend "$SPATIAL_FRONTEND" > "$TEST_TMP/route-timing.json"
python3 - "$TEST_TMP/route-timing.json" <<'PY'
import json, sys
r=json.load(open(sys.argv[1]))
assert r['source_values']=={'ethernet_half_bit_period_cycles':2, 'i2c_half_period_cycles':250}
assert all(x['minimum_arrival_spacing']==2 for x in r['ethernet'])
assert [x['consumer_sampling_edge_after_first_sampling_edge'] for x in r['i2c_stretch']]==[2,3,4]
PY
python3 "$HERE/investigations/compare_memory.py" --frontend "$SPATIAL_FRONTEND" > "$TEST_TMP/memory-comparison.json"
python3 - "$TEST_TMP/memory-comparison.json" <<'PY'
import json, sys
report=json.load(open(sys.argv[1])); r={(x['candidate'], x['prefetch_depth']): x for x in report['results']}
assert r['one_1k_bank',0]['actor_deadline_misses']==2
assert r['two_1k_banks',0]['actor_deadline_misses']==0
assert r['one_1k_bank',4]['actor_deadline_misses']==0
assert r['one_1k_bank',4]['max_prefetch_occupancy']==4
s={(x['candidate'], x['prefetch_depth']): x for x in report['phase_sweep']}
assert s['one_1k_bank',0]['phases_with_misses']==13
assert s['one_1k_bank',4]['phases_with_misses']==0
assert report['frame_capacity_probe']['stored_frame_bytes']==60
assert report['frame_capacity_probe']['source_wire_frame_bytes_without_crc_tail']==68
PY
python3 "$HERE/corpus.py" --frontend "$SPATIAL_FRONTEND" --mapper "$CIRCT_MAP" --circt-opt "$CIRCT_OPT" --dialect "$SPATIAL_DIALECT" --output "$TEST_TMP/corpus.json"
python3 - "$TEST_TMP/corpus.json" <<'PY'
import json, sys
report=json.load(open(sys.argv[1])); r=report['summary']
assert r['total_programs'] > 0 and r['frontend_exported'] == r['total_programs']
assert r['dialect_verified'] == r['total_programs']
assert r['control_table_emitted_and_checked'] > 0
assert r['typed_graph_lowering_unsupported'] > 0
uart=next(p for p in report['programs'] if p['program']=='uart.spt')
assert uart['typed_graph']['status']=='structural_graph_only'
assert uart['typed_graph']['assessments']['current_slice']['status']=='infeasible_in_model'
assert uart['typed_graph']['assessments']['hypothetical_uart']['status']=='abstract_placement_found'
PY
python3 "$HERE/corpus.py" --frontend "$SPATIAL_FRONTEND" --mapper "$CIRCT_MAP" --circt-opt "$CIRCT_OPT" --dialect "$SPATIAL_DIALECT" --examples "$HERE/examples" --output "$TEST_TMP/experiment-corpus.json"
python3 - "$TEST_TMP/experiment-corpus.json" <<'PY'
import json, sys
records = {p['program']: p for p in json.load(open(sys.argv[1]))['programs']}
assert records['memory_spi.spt']['typed_graph']['status'] == 'abstract_graph_placed'
assert not records['memory_spi.spt']['typed_graph']['full_source_program_compiled']
assert records['control.spt']['typed_graph']['status'] == 'lowering_unsupported'
PY
mkdir "$TEST_TMP/one-example"
cp "$HERE/examples/control.spt" "$TEST_TMP/one-example/control.spt"
if python3 "$HERE/corpus.py" --frontend "$SPATIAL_FRONTEND" --mapper "$CIRCT_MAP" --circt-opt "$CIRCT_OPT" --dialect "$SPATIAL_DIALECT" --examples "$TEST_TMP/one-example" --output "$TEST_TMP/strict-corpus.json" --require-full; then
  echo 'strict corpus check accepted an incomplete program' >&2; exit 1
fi
rm "$TEST_TMP/one-example/control.spt"
cp "$HERE/examples/control_full.spt" "$TEST_TMP/one-example/control_full.spt"
python3 "$HERE/corpus.py" --frontend "$SPATIAL_FRONTEND" --mapper "$CIRCT_MAP" --circt-opt "$CIRCT_OPT" --dialect "$SPATIAL_DIALECT" --examples "$TEST_TMP/one-example" --output "$TEST_TMP/strict-corpus.json" --require-full
python3 - "$TEST_TMP/strict-corpus.json" <<'PY'
import json, sys
assert json.load(open(sys.argv[1]))['summary']['full_programs_mapped'] == 1
PY
printf 'PASS: registered MLIR verification, flexible selector analysis, image encoding, and rejection\n'
