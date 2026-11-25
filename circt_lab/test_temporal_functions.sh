#!/usr/bin/env bash
# Whole exported functions (spi.transfer, i2c.write_read) and the UART stream
# interface: fixtures match the hardware repo's generic-runner programs
# byte for byte, parameters recompile, and unsupported source is rejected.
set -euo pipefail
if [[ $# != 1 ]]; then echo "usage: $0 CIRCT_MAP" >&2; exit 2; fi
repo=$(cd "$(dirname "$0")/.." && pwd); mapper=$(realpath "$1")
frontend=${SPATIAL_FRONTEND:-$repo/target/debug/spatial}
fixtures="$repo/../protocol-emulator/test/data/compiled"
tmp=$(mktemp -d); trap 'rm -rf "$tmp"' EXIT
compile="$repo/circt_lab/compile_temporal_function.sh"
"$compile" "$repo/examples/spi.spt" spi.transfer a53c81 "$tmp/spi" "$mapper"
"$compile" "$repo/examples/i2c.spt" i2c.write_read '' "$tmp/i2c" "$mapper"
for pair in spi:spi-transfer i2c:i2c-write-read; do
  name=${pair%%:*}; fixture=${pair#*:}
  for suffix in mem program.json plan.json; do cmp "$tmp/$name.$suffix" "$fixtures/$fixture.$suffix"; done
done
"$frontend" frontend-mlir "$repo/examples/uart.spt" > "$tmp/uart.frontend.mlir"
"$mapper" --compile-grid-uart "$tmp/uart.frontend.mlir" "$tmp/uart.mem" > "$tmp/uart.program.json"
"$frontend" test-plan "$repo/examples/uart.spt" > "$tmp/uart.plan.json"
for suffix in mem program.json plan.json; do cmp "$tmp/uart.$suffix" "$fixtures/uart-streams.$suffix"; done
python3 - "$tmp" "$repo/examples" <<'PY'
import json, pathlib, sys
tmp, examples = pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2])
spi = json.loads((tmp / 'spi.program.json').read_text())
i2c = json.loads((tmp / 'i2c.program.json').read_text())
assert spi['full_source_function_compiled'] and spi['sequencer_rows_used'] == 8
assert i2c['full_source_function_compiled'] and i2c['sequencer_rows_used'] == 16
assert set(spi['interface']['functions']) == {'transfer'} and set(i2c['interface']['functions']) == {'write_read'}
spi_src, i2c_src = (examples / 'spi.spt').read_text(), (examples / 'i2c.spt').read_text()
def variant(name, source, old, new):
    assert source.count(old) == 1, (name, old)
    (tmp / f'{name}.spt').write_text(source.replace(old, new))
variant('spi-half16', spi_src, 'const HALF = 8 cycles', 'const HALF = 16 cycles')
variant('spi-no-gap', spi_src, '    wait gap.tick\n', '')
variant('spi-finish-first', spi_src, '    fire begin\n    repeat data.len { wait bits.word }\n    fire finish\n',
        '    fire begin\n    fire finish\n    repeat data.len { wait bits.word }\n')
variant('spi-idle-high', spi_src, 'timer sck_clock { every: HALF, idle: 0,', 'timer sck_clock { every: HALF, idle: 1,')
variant('i2c-half125', i2c_src, 'const HALF = 250 cycles', 'const HALF = 125 cycles')
variant('i2c-write-after-read', i2c_src, '    read(last: 1)\n    stop()\n}\n\n// ─── tests',
        '    read(last: 1)\n    write(address << 1)\n    stop()\n}\n\n// ─── tests')
variant('i2c-no-stop', i2c_src, '    read(last: 1)\n    stop()\n}\n\n// ─── tests', '    read(last: 1)\n}\n\n// ─── tests')
variant('i2c-last-in-loop', i2c_src, 'repeat count - 1 { read(last: 0) }', 'repeat count - 1 { read(last: 1) }')
variant('i2c-changed-byte', i2c_src, 'fn byte(value: u8, ninth: bit) {', 'fn byte(value: u8, ninth: bit) {\n    fire cond_go')
PY
"$compile" "$tmp/spi-half16.spt" spi.transfer 01 "$tmp/spi16" "$mapper"
"$compile" "$tmp/i2c-half125.spt" i2c.write_read '' "$tmp/i2c125" "$mapper"
python3 - "$tmp" <<'PY'
import json, sys, pathlib
tmp = pathlib.Path(sys.argv[1])
s16, s8 = (json.loads((tmp / f).read_text()) for f in ('spi16.program.json', 'spi.program.json'))
assert s16['base_words']['0'] == s16['base_words']['53'] == 16 and s16['temporal'] == s8['temporal']
i125, i250 = (json.loads((tmp / f).read_text()) for f in ('i2c125.program.json', 'i2c.program.json'))
assert i125['base_words']['0'] == 125 and i125['temporal'] == i250['temporal']
PY
for case in spi-no-gap:spi.transfer spi-finish-first:spi.transfer spi-idle-high:spi.transfer \
            i2c-write-after-read:i2c.write_read i2c-no-stop:i2c.write_read i2c-last-in-loop:i2c.write_read i2c-changed-byte:i2c.write_read; do
  name=${case%%:*}; fn=${case#*:}; arg=''; [[ $fn == spi.transfer ]] && arg=01
  if "$compile" "$tmp/$name.spt" "$fn" "$arg" "$tmp/bad-$name" "$mapper" > "$tmp/error" 2>&1; then
    echo "unsupported source $name unexpectedly compiled" >&2; exit 1
  fi
done
echo 'Temporal function lowering (SPI transfer, I2C write_read), UART stream interface, fixtures and rejection tests passed'
