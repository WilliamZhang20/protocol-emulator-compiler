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
from fabric_map import MapError, compile_designs, hexfile

HERE = Path(__file__).resolve().parent
FRONTEND = HERE.parent / 'target/debug/spatial'


def plan(frontend, source):
    out = subprocess.run([str(frontend), 'test-plan', str(source)], capture_output=True, text=True)
    if out.returncode:
        raise LowerError('compiler', f'test-plan: {out.stderr.strip()[:300]}')
    return json.loads(out.stdout)


def host_plans(contract, design, r):
    """Resolve each function's host plan against the placement: a host
    event becomes its index (mesh.v host_ev), a waited tile output its
    sticky flag (cell, local), a parameter write its cell and register."""
    nl, binding, name = r['netlist'], r['binding'], design['name']
    out = {}
    for fn, plan in design['functions'].items():
        steps = []
        for st in plan['steps']:
            st = dict(st)
            if st['op'] == 'fire':
                st['index'] = nl.host_events.index(f'{name}/{st["event"]}')
            elif st['op'] == 'wait':
                tile, local = st['signal'].rsplit('.', 1)
                cell = binding[f'{name}/{tile}']
                kind = contract['cells'][cell]['kind']
                st['flag'] = 4 * cell + contract['kinds'][kind]['locals'].index(local)
            elif st['op'] == 'param':
                cell = binding[f'{name}/{st["tile"]}']
                kind = contract['cells'][cell]['kind']
                st['cell'] = cell
                st['addr'] = contract['registers'][kind][st['reg']]['reg']
            steps.append(st)
        out[fn] = dict(plan, steps=steps)
    return out


def build(contract, source, frontend=FRONTEND):
    """Returns (vector bits, plan, interface, compile result)."""
    p = plan(frontend, source)
    ir = model_ir(frontend, source)
    design = lower(ir, Path(source).stem, p['pins'], contract)
    try:
        r = compile_designs(contract, [design])
    except MapError:
        # With logic cells, registers can be LUTs instead of FSMs on contexted
        # switches (exact, and no context-selected routes): the second choice
        # when the first does not place or route.
        if not any(c['kind'] == 'logic' for c in contract['cells']):
            raise
        design = lower(ir, Path(source).stem, p['pins'], contract, prefer_lut=True)
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
    if design.get('functions'):
        interface['functions'] = host_plans(contract, design, r)
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
