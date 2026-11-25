#!/usr/bin/env bash
set -euo pipefail
if [[ $# != 1 ]]; then echo "usage: $0 CIRCT_MAP" >&2; exit 2; fi
repo=$(cd "$(dirname "$0")/.." && pwd); mapper=$(realpath "$1")
tmp=$(mktemp -d); trap 'rm -rf "$tmp"' EXIT
base="$repo/../protocol-emulator/test/data/grid-spi.mem"
compile="$repo/circt_lab/compile_jtag_test.sh"; name='reset and read a 32-bit IDCODE'
"$compile" "$repo/examples/jtag.spt" "$name" "$tmp/ref" "$mapper" "$base"
cmp "$tmp/ref.mem" "$repo/../protocol-emulator/test/data/grid-jtag-temporal.mem"
cmp "$tmp/ref.program.json" "$repo/../protocol-emulator/test/data/grid-jtag-idcode.json"
"$compile" "$repo/examples/jtag.spt" 'select IR and write and read the user DR' "$tmp/user" "$mapper" "$base"
cmp "$tmp/user.program.json" "$repo/../protocol-emulator/test/data/grid-jtag-user.json"
python3 - "$repo/examples/jtag.spt" "$tmp" <<'PY'
from pathlib import Path
import sys
source=Path(sys.argv[1]).read_text(); tmp=Path(sys.argv[2])
old='expect scan8(0, 0) == [0x77]'; assert old in source
(tmp/'alternate.spt').write_text(source.replace(old,'expect scan8(0, 1) == [0x77]',1))
old='shift: clock.fall'; assert old in source
(tmp/'unsupported.spt').write_text(source.replace(old,'shift: clock.rise',1))
old='    step(1, 0)\n    step(0, 0)\n}\n\ntest "select IR'; assert old in source
(tmp/'unsafe-padding.spt').write_text(source.replace(old,'    step(1, 0)\n}\n\ntest "select IR',1))
PY
"$compile" "$tmp/alternate.spt" "$name" "$tmp/alternate" "$mapper" "$base"
python3 - "$tmp/ref.program.json" "$tmp/alternate.program.json" <<'PY'
import json,sys
x,y=(json.load(open(p)) for p in sys.argv[1:]); assert x['payload_bytes']!=y['payload_bytes']; assert x['checks']==y['checks']
PY
for mutation in unsupported unsafe-padding; do
 if "$compile" "$tmp/$mutation.spt" "$name" "$tmp/reject" "$mapper" "$base" >"$tmp/error" 2>&1; then
   echo "unexpectedly accepted $mutation" >&2; exit 1
 fi
done
echo 'JTAG source test lowering, component, mutation and safe padding passed'
