#!/usr/bin/env python3
"""Every blocker of every refused program, not only the first.

fabric_lower stops at its first LowerError. This survey lowers each part of
a program on its own (each register, each function, each machine, each
resource, each output pin) and records every refusal with its class, so the
next lowering step is chosen by what it unblocks, and hardware limits are
separated from compiler gaps:

  python3 blocker_survey.py [--contract C.json] [examples/*.spt ...]

A part that depends on another refused part reports its own first blocker
(a function refused early hides its later statements), so the counts are a
lower bound per part, not a full proof of feasibility.
"""
import argparse
import json
from collections import Counter
from pathlib import Path

from fabric_build import FRONTEND, plan
from fabric_lower import Lowering, LowerError, model_ir
from test_fabric_lower import EXAMPLES, test_mesh


def survey(contract, source, frontend):
    p = plan(frontend, source)
    ir = model_ir(frontend, source)
    lw = Lowering(ir, Path(source).stem, p['pins'])
    lw.latency = contract['latency']
    lw.registers = contract['registers']
    lw.limits = {'states': contract['layout']['contexts'], 'transitions': contract['layout']['transition_rows']}
    lw.tiles, lw.connect = {}, {}
    lw.mode_route = {}
    out = []

    def attempt(part, fn):
        try:
            fn()
        except LowerError as e:
            out.append((part, e.cls, str(e).split(': ', 1)[-1] if str(e).startswith(e.cls) else str(e)))
        except (KeyError, TypeError, AttributeError) as e:      # a part that needs a refused part
            out.append((part, 'depends', f'{type(e).__name__}: {e}'))

    items = lw.items
    functions = [i for i in items if i['kind'] == 'function']
    others = [i for i in items if i['kind'] != 'function']
    collected = {'plans': {}, 'fn_modes': {}, 'fn_runs': {}}
    for fn in functions:
        lw.items = others + [fn]

        def one():
            lw.functions()
            collected['plans'].update(lw.plans)
            collected['fn_modes'].update(lw.fn_modes)
        attempt(f'fn {fn["name"]}', one)
    lw.items = items
    lw.plans, lw.fn_modes = collected['plans'], collected['fn_modes']
    lw.tx_via = getattr(lw, 'tx_via', [])
    lw.rx_via = getattr(lw, 'rx_via', [])
    lw.loaded = getattr(lw, 'loaded', set())
    lw.param_tiles = getattr(lw, 'param_tiles', set())
    attempt('modes', lw.mode_machine)
    for name in lw.regs:
        lw.reg_sig = {}
        attempt(f'reg {name}', lambda n=name: lw.register(n))
    for m in lw.machines:
        attempt(f'machine {m}', lambda n=m: lw.machine(n))
    for name, r in lw.res.items():
        kind = r['resource_kind']
        fields = {f['name']: f['value'] for f in r['fields']}

        def tile(name=name, r=r, kind=kind, fields=fields):
            ports = lw.conns(name)
            if kind == 'timer':
                lw.tiles[name] = lw.timer(name, r, fields, ports, lw.connect)
            elif kind == 'shifter':
                lw.tiles[name] = lw.shifter(name, fields, ports, lw.connect)
            elif kind == 'crc':
                lw.tiles[name] = lw.crc(name, fields, ports, lw.connect)
            elif kind == 'counter':
                lw.tiles[name] = lw.counter(name, fields, ports, lw.connect)
            else:
                raise LowerError('hardware', f'{name}: a {kind} resource has no fabric tile')
        attempt(f'{kind} {name}', tile)
    attempt('streams', lambda: lw.streams(lw.tiles))
    for name, pin in lw.pins.items():
        if pin['mode'] in ('out', 'open_drain'):
            def drive(name=name):
                d = lw.conns(name).get('pin.drive')
                if d is None:
                    raise LowerError('compiler', f'output pin {name} is not driven')
                lw.signal(d)
            attempt(f'pin {name}', drive)
    for i in items:
        if i['kind'] in ('flags', 'context', 'context_selector'):
            out.append((f'{i["kind"]} {i.get("name", "")}'.strip(), 'compiler', f'{i["kind"]} not lowered yet (L2/L3)'))
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('sources', nargs='*', type=Path)
    ap.add_argument('--frontend', type=Path, default=FRONTEND)
    ap.add_argument('--json', type=Path, help='write the survey as JSON')
    a = ap.parse_args()
    contract = test_mesh()
    sources = a.sources or sorted(EXAMPLES.glob('*.spt')) + sorted(EXAMPLES.glob('fabric/*.spt'))
    report, kinds = {}, Counter()
    for src in sources:
        try:
            blockers = survey(contract, src, a.frontend)
        except LowerError as e:
            blockers = [('program', e.cls, str(e))]
        report[src.stem] = blockers
        if not blockers:
            print(f'{src.stem}: no blockers')
            continue
        print(f'{src.stem}: {len(blockers)} blockers')
        for part, cls, msg in blockers:
            print(f'    {cls:9s} {part:28s} {msg[:150]}')
            kinds[(cls, msg.split(':')[0][:60] if cls != 'compiler' else ' '.join(msg.split()[:6]))] += 1
    if a.json:
        a.json.write_text(json.dumps(report, indent=1) + '\n')


if __name__ == '__main__':
    main()
