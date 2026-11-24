#!/usr/bin/env bash
# End-to-end source-profile and negative tests; pass a built CIRCT mapper.
set -euo pipefail
if [[ $# != 1 ]]; then echo "usage: $0 CIRCT_MAP" >&2; exit 2; fi
repo=$(cd "$(dirname "$0")/.." && pwd)
mapper=$(realpath "$1")
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
compile="$repo/circt_lab/compile_i2c_write.sh"
"$compile" "$repo/examples/i2c.spt" 0x48 a6 "$tmp/reference" "$mapper"
cmp "$tmp/reference.mem" "$repo/../protocol-emulator/test/data/temporal-i2c.mem"
"$compile" "$repo/examples/i2c.spt" 0x50 3c "$tmp/alternate" "$mapper"
"$compile" "$repo/examples/i2c.spt" 0x50 '' "$tmp/empty" "$mapper"
python3 - "$tmp" "$repo/examples/i2c.spt" <<'PY'
import json, pathlib, sys
p=pathlib.Path(sys.argv[1]); source=pathlib.Path(sys.argv[2]).read_text()
ref=json.loads((p/'reference.program.json').read_text())
alt=json.loads((p/'alternate.program.json').read_text())
empty=json.loads((p/'empty.program.json').read_text())
assert ref['payload_bytes']==[1,33,1,77] and ref['base_words']['66']==4
assert alt['payload_bytes']==[1,65,0,121] and alt['base_words']['67']==4
assert empty['payload_bytes']==[1,65] and empty['base_words']['66']==2
assert ref['rows']==alt['rows']==empty['rows']
assert 'const HALF = 250 cycles' in source and 'byte(value, ninth: 1)' in source
(p/'fast.spt').write_text(source.replace('const HALF = 250 cycles','const HALF = 125 cycles'))
(p/'unsupported.spt').write_text(source.replace('byte(value, ninth: 1)','byte(value, ninth: 0)'))
PY
"$compile" "$tmp/fast.spt" 0x48 a6 "$tmp/fast" "$mapper"
python3 - "$tmp/fast.program.json" <<'PY'
import json,sys
p=json.load(open(sys.argv[1])); assert p['base_words']['0']==125 and p['base_words']['1']==125
PY
if "$compile" "$tmp/unsupported.spt" 0x48 a6 "$tmp/invalid" "$mapper" >"$tmp/error" 2>&1; then
    echo 'unsupported source unexpectedly compiled' >&2; exit 1
fi
if "$compile" "$repo/examples/i2c.spt" 0x80 a6 "$tmp/invalid-address" "$mapper" >"$tmp/error" 2>&1; then
    echo 'invalid address unexpectedly compiled' >&2; exit 1
fi
echo 'I2C source profile, CIRCT routing, specialization and rejection tests passed'
