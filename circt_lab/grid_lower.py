#!/usr/bin/env python3
"""General lowering of a source program to one lane core (step 5, R7).

Input: the frontend's logical IR (`spatial model-ir FILE.spt`): resources
with their fields and timings, pins, streams, and every resource input
resolved to the signal it references. Output: a lane design (schema v2,
grid_lane.py builds it) and its controller program. Nothing here names a
protocol: each construct maps by a rule, and a construct a lane core
cannot hold is refused with the reason (the step-5.4 diagnostics).

Rules:
- in pins -> pad inputs in0, in1 in declaration order; their `rise`,
  `fall` and level map to the core's lines; out pins -> outputs out0-2,
  driven by the expression the pin names, enable high.
- timers (at most two) -> timer0, timer1: `every` -> period, `first` ->
  first (source counts are taken as raw counts for now: see
  PLACE_ROUTE.md, "R7 design question"); `run: A until B` -> start A,
  stop B.
- one shifter -> the coded shifter: the frame list `[bits.., data(n),
  odd|even, bits..]` -> pre bits, length, parity, post bits; `idle`,
  `start`, `shift`, `sample: X at E` -> start, shift, in X, sample E.
- a stream from the shifter to the host -> a receive program: wait for
  each word, fail on a bad word (parity or post bit), push it; `count`
  words, then halt.
"""
import argparse
import json
from pathlib import Path


class LowerError(ValueError):
    pass


def _value(expr):
    if expr is None:
        return None
    if expr['kind'] == 'integer':
        return int(expr['value'])
    raise LowerError(f'expected an integer, got {expr["kind"]}')


class Lowering:
    def __init__(self, ir, count=None, name=None):
        self.ir = ir
        self.items = ir['items']
        self.inputs = ir['connections']['inputs']
        self.name = name or Path(ir.get('source', 'program')).stem
        self.count = count
        self.pins_in, self.pins_out = [], []
        for i in self.items:
            if i['kind'] == 'pin':
                (self.pins_in if i['mode'] == 'in' else self.pins_out).append(i['name'])
        if len(self.pins_in) > 2:
            raise LowerError(f'{len(self.pins_in)} input pins; a lane core has 2 pad inputs')
        if len(self.pins_out) > 3:
            raise LowerError(f'{len(self.pins_out)} output pins; a lane core has 3 outputs')
        res = [i for i in self.items if i['kind'] == 'resource']
        self.timers = [r for r in res if r['resource_kind'] == 'timer']
        self.shifters = [r for r in res if r['resource_kind'] == 'shifter']
        other = [r['name'] + ' (' + r['resource_kind'] + ')' for r in res if r['resource_kind'] not in ('timer', 'shifter')]
        if other:
            raise LowerError(f'not lowered to a lane core yet: {", ".join(other)}')
        if len(self.timers) > 2:
            raise LowerError(f'{len(self.timers)} timers; a lane core has 2')
        if len(self.shifters) > 1:
            raise LowerError(f'{len(self.shifters)} shifters; a lane core has one coded shifter '
                             '(a protocol needing more takes two lanes, step 2.6)')
        self.timer_slot = {t['name']: f'timer{k}' for k, t in enumerate(self.timers)}

    # ---- signals
    def line(self, conn):
        """The core line an input connection reads."""
        expr, refs = conn['expression'], conn['references']
        if not refs:
            v = _value(expr)
            if v not in (0, 1):
                raise LowerError(f'{conn["consumer"]}.{conn["port"]}: constant {v}')
            return 'high' if v else 'low'
        if len(refs) != 1:
            raise LowerError(f'{conn["consumer"]}.{conn["port"]}: an expression of {len(refs)} signals '
                             'needs logic (LUT4 lowering is step 5.3)')
        ref = refs[0]
        kind, name, out = ref['resolution'], ref['name'], ref['output']
        if kind == 'pin':
            if name not in self.pins_in:
                raise LowerError(f'{name} is not an input pin')
            pad = f'in{self.pins_in.index(name)}'
            return pad if out is None else f'{pad}.{out}'
        if kind == 'timer':
            mapped = {'tick': 'tick', 'rise': 'rise', 'fall': 'fall', 'level': 'level', 'running': 'running'}
            if out not in mapped:
                raise LowerError(f'timer output {out}')
            return f'{self.timer_slot[name]}.{mapped[out]}'
        if kind == 'shifter':
            mapped = {'out': 'coder.serial', 'busy': 'coder.active', 'done': 'coder.stop', 'word': 'coder.word'}
            if out not in mapped:
                raise LowerError(f'shifter output {out}')
            return mapped[out]
        raise LowerError(f'{conn["consumer"]}.{conn["port"]}: a {kind} signal is not on a lane core')

    def conns(self, consumer):
        return {c['port']: c for c in self.inputs if c['consumer'] == consumer and c['context'] is None}

    # ---- resources
    def lower(self):
        connect, timers, coder = {}, {}, {}
        for t in self.timers:
            slot = self.timer_slot[t['name']]
            timing = dict(t['timings'])
            spec = {}
            for src, dst in (('every', 'period'), ('first', 'first')):
                if src in timing:
                    v = timing[src]
                    if v['status'] != 'known' or v['denominator'] != '1':
                        raise LowerError(f'{t["name"]}.{src}: not a whole number of cycles')
                    spec[dst] = int(v['numerator'])
            if 'period' in spec and 'first' not in spec:
                spec['first'] = spec['period']
            timers[slot] = spec
            ports = self.conns(t['name'])
            for port, target in (('timer.start', 'start'), ('timer.stop', 'stop')):
                if port in ports:
                    connect[f'{slot}.{target}'] = self.line(ports[port])
            extra = set(ports) - {'timer.start', 'timer.stop'}
            if extra:
                raise LowerError(f'{t["name"]}: inputs {sorted(extra)} not lowered yet')
        if self.shifters:
            s = self.shifters[0]
            fields = {f['name']: f['value'] for f in s['fields']}
            coder['frame'] = self.frame(s['name'], fields)
            ports = self.conns(s['name'])
            for port, target in (('shifter.start', 'coder.start'), ('shifter.shift', 'coder.shift'),
                                 ('shifter.sample_at', 'coder.sample'), ('shifter.sample_data', 'coder.in')):
                if port in ports:
                    connect[target] = self.line(ports[port])
            extra = set(ports) - {'shifter.start', 'shifter.shift', 'shifter.sample_at', 'shifter.sample_data'}
            if extra:
                raise LowerError(f'{s["name"]}: inputs {sorted(extra)} not lowered yet')
        for k, pin in enumerate(self.pins_out):
            drive = self.conns(pin).get('pin.drive')
            if drive is None:
                raise LowerError(f'output pin {pin} is not driven')
            connect[f'out{k}.value'] = self.line(drive)
            connect[f'out{k}.enable'] = 'high'
        program = self.program()
        design = {'schema': 'spatial.lane-design.v2', 'name': self.name,
                  'description': f'Lowered from {self.ir.get("source", "source")} by grid_lower.py.',
                  'connect': connect, 'timers': timers, 'coder': coder,
                  'host_words': {'68': 3}, 'program_base': 768}
        if not timers:
            del design['timers']
        if not coder:
            del design['coder']
        return design, program

    def frame(self, name, fields):
        f = fields.get('frame')
        if f is None or f['kind'] != 'list':
            raise LowerError(f'{name}: no frame list')
        pre, post, length, parity, seen_data = [], [], None, None, False
        for v in f['values']:
            if v['kind'] == 'integer':
                (post if seen_data else pre).append(int(v['value']))
            elif v['kind'] == 'call' and v['callee'].get('name') == 'data':
                if seen_data:
                    raise LowerError(f'{name}: two data fields in one frame')
                seen_data = True
                length = _value(v['args'][0]['value'])
            elif v['kind'] == 'name' and v['name'] in ('odd', 'even'):
                if not seen_data or post:
                    raise LowerError(f'{name}: parity goes right after the data')
                parity = v['name']
            else:
                raise LowerError(f'{name}: frame element {v["kind"]} not lowered')
        if not seen_data:
            raise LowerError(f'{name}: frame without data')
        for bits, what in ((pre, 'leading'), (post, 'trailing')):
            if len(set(bits)) > 1:
                raise LowerError(f'{name}: {what} bits {bits} differ; the coder repeats one value')
        out = {'length': length}
        if pre:
            out.update(pre=len(pre), pre_value=pre[0])
        if post:
            out.update(post=len(post), post_value=post[0])
        if parity:
            out['parity'] = parity
        if 'idle' in fields:
            out['idle'] = _value(fields['idle'])
        return out

    def program(self):
        streams = [i for i in self.items if i['kind'] == 'stream']
        rx = [s for s in streams if s['to'].get('name') == 'host']
        tx = [s for s in streams if s['from'].get('name') == 'host']
        if tx:
            raise LowerError('host-to-pin streams are not lowered to a lane core yet')
        if len(rx) != 1:
            raise LowerError(f'{len(rx)} receive streams; this lowering takes one')
        if self.count is None or not 1 <= self.count <= 255:
            raise LowerError('a receive program needs a word count of 1-255')
        return '\n'.join([
            f'# Receive {self.count} words from the coded shifter to the host stream.',
            f'        SETC {self.count}',
            'next:   WAIT event:coder.word',
            '        RD readback:coder.status',
            '        AND 0x40                    # word bad: parity or a post bit',
            '        JNZ bad',
            '        RD readback:coder.rx_word',
            '        PUSH',
            '        LOOP next',
            '        HALT',
            'bad:    FAIL', ''])


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('ir', type=Path, help='spatial model-ir output (JSON)')
    ap.add_argument('outdir', type=Path)
    ap.add_argument('--count', type=int)
    ap.add_argument('--name')
    args = ap.parse_args()
    ir = json.loads(args.ir.read_text())
    design, program = Lowering(ir, args.count, args.name).lower()
    args.outdir.mkdir(parents=True, exist_ok=True)
    (args.outdir / f'{design["name"]}.lane').write_text(program)
    design['program'] = f'{design["name"]}.lane'
    (args.outdir / f'{design["name"]}.json').write_text(json.dumps(design, indent=2) + '\n')
    print(args.outdir / f'{design["name"]}.json')


if __name__ == '__main__':
    main()
