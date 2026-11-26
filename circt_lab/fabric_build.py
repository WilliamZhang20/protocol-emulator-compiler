#!/usr/bin/env python3
"""Compile a .spt program for the reorganised fabric: lower (fabric_lower),
place, route, emit and check (fabric_map), and write what the RTL
repository's mesh runner (test/cocotb_tests/test_mesh_compiled.py) needs:

  NAME.hex        the configuration vector ($readmemh, 32-bit words)
  NAME.plan.json  the source's tests (`spatial test-plan`)
  NAME.mesh.json  the mesh parameters and the program's interface: which
                  mesh port each host stream uses

Pads are the plan's logical pins, so the plan's peers and wires attach to
the same numbers the configuration drives.
"""
import json
import subprocess
from pathlib import Path

from fabric_arch import verilog_params
from fabric_lower import LowerError, lower, model_ir
from fabric_map import compile_designs, hexfile

HERE = Path(__file__).resolve().parent
FRONTEND = HERE.parent / 'target/debug/spatial'


def plan(frontend, source):
    out = subprocess.run([str(frontend), 'test-plan', str(source)], capture_output=True, text=True)
    if out.returncode:
        raise LowerError('compiler', f'test-plan: {out.stderr.strip()[:300]}')
    return json.loads(out.stdout)


def build(contract, source, frontend=FRONTEND):
    """Returns (vector bits, plan, interface, compile result)."""
    p = plan(frontend, source)
    ir = model_ir(frontend, source)
    design = lower(ir, Path(source).stem, p['pins'], contract)
    r = compile_designs(contract, [design])
    name = design['name']
    streams = {}
    for i in ir['items']:
        if i['kind'] != 'stream':
            continue
        if i['from'].get('name') == 'host':
            streams[i['name']] = {'port': 'reader'}
        else:
            sh = f'{name}/{i["from"]["name"]}'
            nl = r['netlist']
            if nl.words and nl.words.get('rx_shifter') == sh:
                streams[i['name']] = {'port': 'writer'}
            else:
                streams[i['name']] = {'port': 'host', 'cell': r['binding'][sh]}
    interface = {'contract': contract['name'], 'params': verilog_params(contract), 'streams': streams,
                 'clock_hz': p['clock_hz']}
    return r['bits'], p, interface, r


def write(contract, source, stem, frontend=FRONTEND):
    bits, p, interface, r = build(contract, source, frontend)
    stem = str(stem)
    Path(stem + '.hex').write_text(hexfile(contract, bits))
    Path(stem + '.plan.json').write_text(json.dumps(p, indent=1) + '\n')
    Path(stem + '.mesh.json').write_text(json.dumps(interface, indent=1) + '\n')
    return r


if __name__ == '__main__':
    import argparse
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('contract', type=Path)
    ap.add_argument('source', type=Path)
    ap.add_argument('stem', type=Path, help='output path without suffix')
    ap.add_argument('--frontend', type=Path, default=FRONTEND)
    a = ap.parse_args()
    r = write(json.loads(a.contract.read_text()), a.source, a.stem, a.frontend)
    print(f'{a.source.name}: placed ({r["seed"]}), {r["tracks_used"]} track segments, check passed')
