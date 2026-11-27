#!/usr/bin/env python3
"""Source -> fabric lowering (FABRIC_REORG.md step 4, L1-L2) and the
source-to-pins build (fabric_build.py).

- The tile register field maps (fabric_arch.REGISTERS) agree with the
  hardware's own (architecture/grid_5x3.json lane_core.registers) on every
  field the hardware publishes.
- Every example program either compiles (lowered, placed, routed and
  passed the independent checker) or is refused with a class; nothing
  fails any other way. The table is the current coverage of the examples.
- The programs the RTL repository runs at the mesh's pins through its SRAM
  (experiments/grid_fabric/reorg/compiled/*.plan.json, make
  reorg-compiled-test) are exactly what this compiler writes.
"""
import json
import os
import tempfile
from pathlib import Path

from fabric_arch import REGISTERS, test_mesh
from fabric_build import FRONTEND, build, write
from fabric_lower import LowerError
from fabric_map import MapError

HERE = Path(__file__).resolve().parent
EXAMPLES = HERE.parent / 'examples'
# Programs that must compile today; the rest of the table may only grow.
MUST_COMPILE = {'i2c_target', 'i2c_uart_tx', 'loopback', 'ps2', 'spi', 'uart'}


def check_registers(rtl):
    hw = json.loads((rtl / 'architecture/grid_5x3.json').read_text())['lane_core']['registers']
    tiles = {'coder': 'shifter', 'crc': 'crc'}
    checked = 0
    for key, spec in hw.items():
        tile, reg = key.split('.')
        ours = REGISTERS[tiles[tile]].get(reg)
        if 'fields' not in spec:
            continue
        assert ours, f'{key}: the hardware publishes fields this compiler does not know'
        for field, bits in spec['fields'].items():
            assert ours['fields'].get(field) == bits, f'{key}.{field}: hardware {bits}, compiler {ours["fields"].get(field)}'
            checked += 1
        for enum in set(spec) - {'word', 'words', 'fields'}:
            assert ours.get(enum) == spec[enum], f'{key}.{enum}: encodings differ'
    print(f'register maps agree with the hardware on {checked} fields')


def main():
    frontend = Path(os.environ.get('SPATIAL_FRONTEND', FRONTEND))
    rtl = os.environ.get('RTL_ROOT')
    rtl = Path(rtl) if rtl and (Path(rtl) / 'architecture/grid_5x3.json').exists() else None
    if rtl:
        check_registers(rtl)
    contract = test_mesh()
    compiled, refused = set(), {'hardware': 0, 'compiler': 0}
    for src in sorted(EXAMPLES.glob('*.spt')) + sorted(EXAMPLES.glob('fabric/*.spt')):
        try:
            r = build(contract, src, frontend)[3]
            compiled.add(src.stem)
            result = f'compiled: {r["tracks_used"]} track segments, longest route {r["max_hops"]} hops, check passed'
        except LowerError as e:
            refused[e.cls] += 1
            result = f'refused ({e.cls}): {str(e).split(": ", 1)[-1]}'
        except MapError as e:
            refused.setdefault(f'map {e.cls}', 0)
            refused[f'map {e.cls}'] += 1
            result = f'map refused ({e.cls}): {e}'
        print(f'  {src.stem:38s} {result[:140]}')
    missing = MUST_COMPILE - compiled
    assert not missing, f'no longer compiles: {sorted(missing)}'
    print(f'coverage on {contract["name"]}: {len(compiled)} compiled, '
          + ', '.join(f'{n} refused ({k})' for k, n in refused.items()))

    committed = rtl / 'experiments/grid_fabric/reorg/compiled' if rtl else None
    if committed and committed.exists():
        plans = sorted(committed.glob(f'*.{contract["name"]}.plan.json'))
        assert {p.name.split('.')[0] for p in plans} == MUST_COMPILE, \
            f'RTL repository runs {[p.name for p in plans]}, expected {sorted(MUST_COMPILE)}'
        with tempfile.TemporaryDirectory() as tmp:
            for p in plans:
                name = p.name.split('.')[0]
                stem = f'{name}.{contract["name"]}'
                src = next(p for p in (EXAMPLES / f'{name}.spt', EXAMPLES / 'fabric' / f'{name}.spt') if p.exists())
                write(contract, src, Path(tmp) / stem, frontend)
                for suffix in ('.hex', '.plan.json', '.mesh.json'):
                    assert (Path(tmp) / (stem + suffix)).read_text() == (committed / (stem + suffix)).read_text(), \
                        f'RTL repository holds a stale {stem}{suffix}: rerun fabric_build.py'
        print(f'RTL repository programs match this compiler: {", ".join(sorted(MUST_COMPILE))}')
    else:
        print('RTL_ROOT not set or older hardware pin: committed-program check skipped')
    print('PASS fabric lowering')


if __name__ == '__main__':
    main()
