#!/usr/bin/env python3
"""Lower a source program to fabric IR (FABRIC_REORG.md step 4, L1-L2):
the frontend's logical IR (`spatial model-ir FILE.spt`) becomes the
design fabric_map.py places and routes. Nothing here names a protocol;
each construct maps by a rule, and a construct the fabric cannot hold is
refused with its class (LowerError.cls):

  'hardware'  the fabric has no resource for it (e.g. logic on several
              signals that is not a pin qualifier or a context mux);
  'compiler'  this lowering does not handle it yet (a later stage: L3
              functions, L4 registers, contexts).

Rules (L1, L2):
- An input pin's level, rise and fall are I/O-tile outputs (events). A
  pin edge or level qualified by another pin's level (`a.fall && b`,
  `a.rise && !b`) is one qualified I/O output.
- An output pin is a push-pull pad driven by the signal it names (enable
  1); `open_drain` pins drive low when the signal is 0.
- Timers: `every` -> period, `first` -> first (default `every`), `run: A
  until B` -> start A, stop B, `run: 1` -> started at once. Source times
  are measured at the pins: a timer started by a pin edge whose tick
  samples a shifter that samples a pin is shortened by the contract's
  edge_to_sample; one started by a shifter's `busy` that shifts it, by
  start_to_shift.
- Shifters: `frame: [pre.., data(n), odd|even, post..]`, `idle`, `lead`,
  `end_on_sample`, `burst`, `end_on_packet`, the coder fields (as the
  Rust elaborator: stuff_after, stuff_ones, nrzi, code_received,
  arm_at_zero, invert_crc_tail, bit_strobe, crc_tail_bits, manchester,
  crc_skip_bits) -> registers; `start`, `shift`, `sample: X at E`, `abort`
  -> the tile's sinks; `sample` at the shift signal shares the shift line.
- CRCs: width, poly, check, init (ones), link -> registers; `restart` ->
  the init line.
- Streams go through SRAM: host -> shifter is the word network's reader
  (SRAM) into the shifter's transmit FIFO; the first shifter -> host
  stream is the shifter's receive FIFO into the writer (SRAM). A second
  receive stream falls back to the shifter's direct host port (there is
  one writer); a second transmit stream is refused (one reader).
"""
import json
import subprocess
from pathlib import Path


class LowerError(ValueError):
    def __init__(self, cls, msg):
        super().__init__(f'{cls}: {msg}')
        self.cls = cls


def _int(expr, env):
    k = expr['kind']
    if k == 'integer':
        return int(expr['value'], 0) if isinstance(expr['value'], str) else int(expr['value'])
    if k == 'quantity':
        if expr['unit'] not in ('cycle', 'cycles'):
            raise LowerError('compiler', f'quantity in {expr["unit"]}')
        return int(expr['value'])
    if k == 'name' and expr['name'] in env:
        return env[expr['name']]
    if k == 'binary' and expr['op'] in ('/', '*', '+', '-'):
        a, b = _int(expr['left'], env), _int(expr['right'], env)
        return {'/': lambda: a // b, '*': lambda: a * b, '+': lambda: a + b, '-': lambda: a - b}[expr['op']]()
    raise LowerError('compiler', f'not a constant: {json.dumps(expr)[:120]}')


class Lowering:
    def __init__(self, ir, name, pads):
        self.ir, self.name, self.pads = ir, name, dict(pads)
        self.items = ir['items']
        self.inputs = ir['connections']['inputs']
        self.env = {}
        for i in self.items:
            if i['kind'] == 'constant' and i['value']['kind'] in ('integer', 'quantity'):
                self.env[i['name']] = _int(i['value'], {})
        self.pins = {i['name']: i for i in self.items if i['kind'] == 'pin'}
        self.res = {i['name']: i for i in self.items if i['kind'] == 'resource'}
        for p in self.pins:
            if p not in self.pads:
                raise LowerError('compiler', f'pin {p} has no pad assignment')
        self.events, self.design = {}, None

    # ---- signals
    def event(self, pin, edge, qual=None):
        """The I/O output for a pin's level/rise/fall, optionally qualified."""
        if pin not in self.pins or self.pins[pin]['mode'] not in ('in', 'inout', 'open_drain'):
            if pin in self.pins and self.pins[pin]['mode'] == 'out':
                raise LowerError('hardware', f'{pin} is an output pin; its level is not an input')
        key = (pin, edge, qual)
        if key not in self.events:
            name = pin if edge == 'level' else f'{pin}_{edge}'
            if qual:
                name += f'_when_{"" if qual[1] else "not_"}{qual[0]}'
            e = {'pin': pin, 'edge': edge}
            if qual:
                e['when'] = {'pin': qual[0], 'level': qual[1]}
            self.events[key] = (name, e)
        return self.events[key][0]

    def signal(self, conn):
        """The fabric-IR ref a connection reads: an event, `tile.output` or 0/1."""
        expr, refs = conn['expression'], conn['references']
        where = f'{conn["consumer"]}.{conn["port"]}'
        if not refs:
            v = _int(expr, self.env)
            if v not in (0, 1):
                raise LowerError('hardware', f'{where}: constant {v} on a 1-bit input')
            return v
        if len(refs) == 1:
            return self.ref(refs[0], where)
        # Two pin signals combined: an I/O qualifier, if it has that shape.
        q = self.qualifier(expr)
        if q:
            return self.event(*q)
        names = ', '.join(f'{r["name"]}.{r["output"]}' if r['output'] else r['name'] for r in refs)
        raise LowerError('hardware', f'{where}: logic on {len(refs)} signals ({names}); the fabric has no logic '
                         'cells (only I/O qualifiers and context muxes, L3/L4)')

    def qualifier(self, expr):
        if expr['kind'] != 'binary' or expr['op'] != '&&':
            return None
        for a, b in ((expr['left'], expr['right']), (expr['right'], expr['left'])):
            edge = self.pin_term(a)
            lev = self.pin_level(b)
            if edge and lev and edge[0] != lev[0]:
                return edge[0], edge[1], lev
        return None

    def pin_term(self, e):
        if e['kind'] == 'field' and e['object'].get('kind') == 'name' and e['object']['name'] in self.pins \
                and e['field'] in ('rise', 'fall'):
            return e['object']['name'], e['field']
        if e['kind'] == 'name' and e['name'] in self.pins:
            return e['name'], 'level'
        return None

    def pin_level(self, e):
        if e['kind'] == 'name' and e['name'] in self.pins:
            return e['name'], 1
        if e['kind'] == 'unary' and e['op'] == '!' and e['value']['kind'] == 'name' and e['value']['name'] in self.pins:
            return e['value']['name'], 0
        return None

    def ref(self, r, where):
        kind, name, out = r['resolution'], r['name'], r['output']
        if kind == 'pin':
            return self.event(name, out or 'level')
        if kind == 'timer':
            if out not in ('tick', 'level', 'rise', 'fall'):
                raise LowerError('compiler', f'{where}: timer output {out}')
            return f'{name}.{out}'
        if kind == 'shifter':
            mapped = {'out': 'out', 'busy': 'active', 'done': 'stop', 'word': 'word'}
            if out not in mapped:
                raise LowerError('compiler', f'{where}: shifter output {out}')
            return f'{name}.{mapped[out]}'
        if kind == 'crc':
            if out != 'ok':
                raise LowerError('compiler', f'{where}: crc output {out}')
            return f'{name}.ok'
        raise LowerError('compiler', f'{where}: a {kind} signal is not lowered yet (L3/L4)')

    def conns(self, consumer):
        out = {}
        for c in self.inputs:
            if c['consumer'] == consumer:
                if c['context'] is not None:
                    raise LowerError('compiler', f'{consumer}.{c["port"]}: context-dependent input (L2 contexts)')
                if c['check']['status'] != 'valid':
                    raise LowerError('compiler', f'{consumer}.{c["port"]}: frontend check {c["check"]["status"]}')
                out[c['port']] = c
        return out

    # ---- resources
    def lower(self):
        unsupported = [i for i in self.items if i['kind'] in ('function', 'register', 'flags', 'context',
                                                                'context_selector', 'events')]
        tiles, connect, drivers = {}, {}, {}
        for name, r in self.res.items():
            kind = r['resource_kind']
            fields = {f['name']: f['value'] for f in r['fields']}
            ports = self.conns(name)
            if kind == 'timer':
                tiles[name] = self.timer(name, r, fields, ports, connect)
            elif kind == 'shifter':
                tiles[name] = self.shifter(name, fields, ports, connect)
            elif kind == 'crc':
                tiles[name] = self.crc(name, fields, ports, connect)
            else:
                raise LowerError('hardware', f'{name}: a {kind} resource has no fabric tile')
        for name, p in self.pins.items():
            if p['mode'] in ('out', 'open_drain'):
                drive = self.conns(name).get('pin.drive')
                if drive is None:
                    raise LowerError('compiler', f'output pin {name} is not driven')
                d = f'{name}_out'
                drivers[d] = {'pin': name, 'push_pull': p['mode'] == 'out'}
                connect[f'{d}.value'] = self.signal(drive)
                connect[f'{d}.enable'] = 1
        words = self.streams(tiles)
        self.pin_reference(tiles, connect)
        design = {'name': self.name, 'about': f'Lowered from {self.ir.get("source", "source")} by fabric_lower.py.',
                  'pins': {p: self.pads[p] for p in self.pins},
                  'events': {n: e for n, e in self.events.values()},
                  'drivers': drivers, 'tiles': tiles, 'connect': connect}
        if words:
            design['words'] = words
        if unsupported:
            kinds = sorted({i['kind'] for i in unsupported})
            raise LowerError('compiler', f'not lowered yet: {", ".join(kinds)} (L3/L4)')
        return design

    def timer(self, name, r, f, ports, connect):
        timing = dict(r['timings'])
        spec = {'kind': 'timer'}
        for src, dst in (('every', 'period'), ('first', 'first')):
            if src in timing:
                v = timing[src]
                if v['status'] != 'known' or v['denominator'] != '1':
                    raise LowerError('compiler', f'{name}.{src}: not a whole number of cycles')
                spec[dst] = int(v['numerator'])
        if 'period' not in spec:
            raise LowerError('compiler', f'{name}: no `every`')
        spec.setdefault('first', spec['period'])
        for port, sink in (('timer.start', 'start'), ('timer.stop', 'stop')):
            if port in ports:
                connect[f'{name}.{sink}'] = self.signal(ports[port])
        if 'run' in f and f['run'] is not None and f['run']['kind'] == 'integer':
            connect[f'{name}.start'] = 1 if _int(f['run'], self.env) else 0
        extra = set(ports) - {'timer.start', 'timer.stop'}
        if extra:
            raise LowerError('compiler', f'{name}: inputs {sorted(extra)} not lowered yet')
        return spec

    def frame(self, name, f):
        fr = f.get('frame')
        if fr is None:
            return {'length_minus_1': 7}
        if fr['kind'] != 'list':
            raise LowerError('compiler', f'{name}: frame is not a list')
        pre, post, length, parity, seen = [], [], None, None, False
        for v in fr['values']:
            if v['kind'] == 'integer':
                (post if seen else pre).append(int(v['value']))
            elif v['kind'] == 'call' and v['callee'].get('name') == 'data':
                seen, length = True, _int(v['args'][0]['value'], self.env)
            elif v['kind'] == 'name' and v['name'] in ('odd', 'even'):
                parity = v['name']
            else:
                raise LowerError('compiler', f'{name}: frame element {v["kind"]}')
        if not seen:
            raise LowerError('hardware', f'{name}: frame without data')
        for bits, what in ((pre, 'leading'), (post, 'trailing')):
            if len(set(bits)) > 1:
                raise LowerError('hardware', f'{name}: {what} bits {bits} differ; the shifter repeats one value')
            if len(bits) > 3:
                raise LowerError('hardware', f'{name}: {len(bits)} {what} bits; at most 3')
        if not 1 <= length <= 16:
            raise LowerError('hardware', f'{name}: {length} data bits; 1-16')
        out = {'length_minus_1': length - 1}
        if pre:
            out.update(pre=len(pre), pre_value=pre[0])
        if post:
            out.update(post=len(post), post_value=post[0])
        if parity:
            out['parity'] = parity
        for flag in ('lead', 'end_on_sample'):
            if flag in f:
                out[flag] = 1 if f[flag] is None else _int(f[flag], self.env)
        if 'idle' in f:
            out['idle'] = _int(f['idle'], self.env)
        if 'msb_first' in f:
            out['msb_first'] = 1
        return out

    def shifter(self, name, f, ports, connect):
        spec = {'kind': 'shifter', 'frame': self.frame(name, f), 'step': {}, 'io': {}}
        flag = lambda k: k in f and (f[k] is None or _int(f[k], self.env) != 0)
        num = lambda k: _int(f[k], self.env) if k in f else 0
        coder = {'stuff_run': num('stuff_after'), 'runs_of_ones': int(flag('stuff_ones')), 'nrzi': int(flag('nrzi')),
                 'receive': int(flag('code_received')), 'arm_at_first_zero': int(flag('arm_at_zero')),
                 'tail_invert': int(flag('invert_crc_tail')), 'bit_strobe': int(flag('bit_strobe')),
                 'tail_length': num('crc_tail_bits'), 'manchester': int(flag('manchester'))}
        if any(coder.values()):
            spec['coder'] = {k: v for k, v in coder.items() if v}
        if 'crc_skip_bits' in f:
            spec['coder2'] = {'crc_skip': num('crc_skip_bits')}
        if 'stuff_while' in f:
            raise LowerError('compiler', f'{name}: stuff_while (a gated stuffing level) not lowered yet')
        burst = {}
        if 'burst' in f:
            burst['burst'] = 1
        if 'end_on_packet' in f:
            burst['eop_ends_burst'] = 1
        if 'end' in f:
            e = f['end']
            if not (e['kind'] == 'integer' or (e['kind'] == 'name' and e['name'] not in self.pins and e['name'] not in self.res)):
                raise LowerError('compiler', f'{name}: a burst-end level from a signal is not lowered yet')
        if burst:
            spec['burst'] = burst
        # Inputs.
        if 'shifter.start' in ports:
            v = self.signal(ports['shifter.start'])
            connect[f'{name}.start'] = v
            spec['io']['start'] = 'start'
        if 'shifter.shift' in ports:
            connect[f'{name}.shift'] = self.signal(ports['shifter.shift'])
            spec['step']['shift'] = 'shift'
        if 'shifter.sample_at' in ports:
            at = self.signal(ports['shifter.sample_at'])
            if at == connect.get(f'{name}.shift'):
                spec['step']['sample'] = 'shift'
            else:
                connect[f'{name}.sample'] = at
                spec['step']['sample'] = 'sample'
        if 'shifter.sample_data' in ports:
            connect[f'{name}.in'] = self.signal(ports['shifter.sample_data'])
            spec['io']['in'] = True
        if 'shifter.abort' in ports:
            connect[f'{name}.abort'] = self.signal(ports['shifter.abort'])
            spec['abort'] = 'abort'
        extra = set(ports) - {'shifter.start', 'shifter.shift', 'shifter.sample_at', 'shifter.sample_data',
                              'shifter.abort'}
        if extra:
            raise LowerError('compiler', f'{name}: inputs {sorted(extra)} not lowered yet')
        return spec

    def crc(self, name, f, ports, connect):
        spec = {'kind': 'crc', 'width': _int(f['width'], self.env), 'poly': _int(f['poly'], self.env)}
        if 'check' in f:
            spec['check'] = _int(f['check'], self.env)
        spec['init_ones'] = True if 'init' not in f else bool(_int(f['init'], self.env))
        if 'link' in f:
            spec['link'] = f['link']['name']
        if 'restart' in ports:
            connect[f'{name}.init'] = self.signal(ports['restart'])
            spec['init'] = 'init'
        extra = set(ports) - {'restart'}
        if extra:
            raise LowerError('compiler', f'{name}: inputs {sorted(extra)} not lowered yet')
        return spec

    def streams(self, tiles):
        streams = [i for i in self.items if i['kind'] == 'stream']
        tx = [s for s in streams if s['from'].get('name') == 'host']
        rx = [s for s in streams if s['to'].get('name') == 'host']
        for s in streams:
            if s.get('stages'):
                raise LowerError('compiler', f'stream {s["name"]}: stages not lowered yet (L5)')
            if s not in tx and s not in rx:
                raise LowerError('compiler', f'stream {s["name"]}: neither end is the host')
        if len(tx) > 1:
            raise LowerError('hardware', f'{len(tx)} transmit streams; the word network has one reader into a shifter')
        words = {}
        for k, s in enumerate(rx):
            sh = s['from']['name']
            tiles[sh].setdefault('io', {})['auto_push'] = True
            if k == 0:                       # the writer: into SRAM
                tiles[sh]['rx_port'] = True
                words['writer'] = 'rx'
        if tx:
            sh = tx[0]['to']['name']
            tiles[sh]['io']['auto_pull'] = True
            tiles[sh]['tx_port'] = True
            words['tx'] = 'reader'
        return words or None

    def pin_reference(self, tiles, connect):
        lat = self.latency
        for name, t in tiles.items():
            if t['kind'] != 'timer':
                continue
            start = connect.get(f'{name}.start')
            tick = f'{name}.tick'
            samples = [s for s, v in tiles.items() if v['kind'] == 'shifter' and
                       connect.get(f'{s}.sample' if v['step'].get('sample') == 'sample' else f'{s}.shift') == tick
                       and isinstance(connect.get(f'{s}.in'), str) and connect[f'{s}.in'] in
                       {n for n, _ in self.events.values()}]
            edge = isinstance(start, str) and any(start == n and e['edge'] in ('rise', 'fall')
                                                  for n, e in self.events.values())
            if edge and samples:
                t['first'] -= lat['edge_to_sample']
            shifted = [s for s, v in tiles.items() if v['kind'] == 'shifter' and connect.get(f'{s}.shift') == tick]
            if isinstance(start, str) and start.endswith('.active') and start[:-7] in shifted:
                t['first'] -= lat['start_to_shift']
            if t['first'] < 1:
                raise LowerError('hardware', f'{name}: first {t["first"]} after the pin latency correction')


def model_ir(frontend, source):
    out = subprocess.run([str(frontend), 'model-ir', str(source)], capture_output=True, text=True)
    if out.returncode:
        raise LowerError('compiler', f'frontend: {out.stderr.strip()[:300]}')
    ir = json.loads(out.stdout)
    ir.setdefault('source', str(source))
    return ir


def lower(ir, name, pads, contract):
    lw = Lowering(ir, name, pads)
    lw.latency = contract['latency']
    return lw.lower()


if __name__ == '__main__':
    import argparse
    p = argparse.ArgumentParser(description='lower a .spt program to fabric IR')
    p.add_argument('contract', type=Path)
    p.add_argument('source', type=Path)
    p.add_argument('--frontend', type=Path, default=Path(__file__).resolve().parent.parent / 'target/debug/spatial')
    p.add_argument('--pads', required=True, help='pin=pad,... e.g. rx=2,tx=3')
    p.add_argument('-o', '--output', type=Path)
    a = p.parse_args()
    pads = {k: int(v) for k, v in (x.split('=') for x in a.pads.split(','))}
    c = json.loads(a.contract.read_text())
    d = lower(model_ir(a.frontend, a.source), a.source.stem, pads, c)
    text = json.dumps(d, indent=1)
    if a.output:
        a.output.write_text(text + '\n')
    else:
        print(text)
