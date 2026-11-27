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
- Counters -> counter tiles (count, clear, a constant `limit`, `streak`
  modes); `.at_least` and `.reached` are its outputs.
- Registers (L4) -> FSMs on contexted switches, by rule:
  - `r = A || (r && !B)` (set wins over clear) is a 2-state FSM
    OFF -A-> ON -B-> OFF; `init 1` makes ON the reset state;
  - `r = A || (r && !(X && Y))` where X can only rise between two A's (a
    counter's `at_least` cleared by A) is OFF -A-> ON -X-> ARMED -Y-> OFF
    (A from any state back to ON): exact because X stays high once risen;
  - `r = s` or `r = !s` for an FSM register s is a mux of s's FSM (the
    register's one-clock lag behind s is not kept: the mux follows s's
    context directly);
  the register's level is a mux of its FSM (1 in ON states). Anything else
  is refused ('hardware': it needs logic).
- Exported functions (L3) -> a host plan (FABRIC_REORG.md step 4: the host
  acts before, between and at quiescent points of a transaction; the
  fabric reacts locally while it runs):
  - `T.limit = expr` before the first fire: a parameter write to the idle
    counter tile;
  - an argument `via S`: the reader stream into S's transmit FIFO; a
    result `via S`: S's receive FIFO through the writer;
  - `fire E`: a host event (an io output in host-event mode);
  - `wait X` on a tile output: the host polls X's sticky flag (cleared at
    the call's start; a second wait on the same output in one call is
    refused, a sticky flag does not count);
  - `repeat N { wait S.word }` where S's received words are the result:
    wait until N result bytes have arrived;
  everything else is refused for now ('compiler'). Whether a host step
  tolerates host latency is measured by the runner (it replays every call
  with added delays); a step that needs wire-timed reaction belongs in the
  fabric, never in the host plan.
- Machines (README, State machines) -> FSMs, one per machine, states in
  source order (the first is the reset state), `on` lines in priority
  order. `m.S` is a mux of m's FSM (1 in S); `if m.S { a } else { b }` is a
  mux of m's FSM (nested selections of other machines nest). A machine
  over the fabric's bounds (4 states, 2 `on` lines per state) is refused
  ('hardware'), never reshaped.
- `compare C on S` -> the word network's compare tap on S's receive
  channel (S must be the receive stream's shifter); `C.match` and
  `C.mismatch` are its pulses. A stream's `gate { open: L }` stage is the
  word network's gate on that channel.
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
    if k == 'binary' and expr['op'] in ('/', '*', '+', '-', '<<', '>>', '|', '&'):
        a, b = _int(expr['left'], env), _int(expr['right'], env)
        return {'/': lambda: a // b, '*': lambda: a * b, '+': lambda: a + b, '-': lambda: a - b,
                '<<': lambda: a << b, '>>': lambda: a >> b, '|': lambda: a | b, '&': lambda: a & b}[expr['op']]()
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
        self.regs = {i['name']: i for i in self.items if i['kind'] == 'register'}
        self.declared = [n for i in self.items if i['kind'] == 'events' for n in i['names']]
        self.host_events, self.fired_in_fabric = [], []
        self.fsms, self.signals, self.reg_sig = {}, {}, {}
        self.plans = {}
        self.machines = {i['name']: i for i in self.items if i['kind'] == 'machine'}
        self.compares = {i['name']: i for i in self.items if i['kind'] == 'compare'}

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
        if len(refs) == 1 and expr['kind'] in ('name', 'field'):
            return self.ref(refs[0], where)
        v = self.expr_signal(expr, where)
        if v is not None:
            return v
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
            if out == 'out':
                out = 'level'          # the timer's square wave (idle 0)
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
        if kind == 'counter':
            if out not in ('at_least', 'reached'):
                raise LowerError('compiler', f'{where}: counter output {out}')
            return f'{name}.{out}'
        if kind == 'register':
            return self.register(name)
        if kind == 'machine':
            return self.state_level(name, out, where)
        if kind == 'compare':
            return self.compare_out(name, out, where)
        if kind == 'event':
            return self.declared_event(name, where)
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

    # ---- machines and compares
    def machine(self, name):
        """The FSM of a machine (built once)."""
        if name in self.fsms:
            return name
        m = self.machines[name]
        states = [st['name'] for st in m['states']]
        if len(set(states)) != len(states):
            raise LowerError('compiler', f'machine {name}: a state is declared twice')
        self.fsms[name] = {'states': states, 'on': []}      # placeholder: breaks reference loops
        for st in m['states']:
            if len(st['on']) > self.limits['transitions']:
                raise LowerError('hardware', f'machine {name}: state {st["name"]} has {len(st["on"])} `on` lines; '
                                 f'this fabric\'s state machines take {self.limits["transitions"]} per state')
        if len(states) > self.limits['states']:
            raise LowerError('hardware', f'machine {name}: {len(states)} states; this fabric\'s state machines '
                             f'hold {self.limits["states"]}')
        rows = []
        for st in m['states']:
            for line in st['on']:
                if line['to'] not in states:
                    raise LowerError('compiler', f'machine {name}: {line["to"]} is not a state')
                sig = self.expr_signal(line['event'], f'machine {name} state {st["name"]}')
                if sig is None:
                    raise LowerError('hardware', f'machine {name} state {st["name"]}: the `on` signal needs logic')
                if sig == 0:
                    continue
                rows.append([st['name'], sig, line['to']])
        self.fsms[name]['on'] = rows
        return name

    def state_level(self, name, state, where):
        if name not in self.machines:
            raise LowerError('compiler', f'{where}: {name} is not a machine')
        if state not in [st['name'] for st in self.machines[name]['states']]:
            raise LowerError('compiler', f'{where}: {name} has no state {state}')
        self.machine(name)
        return {'fsm': name, 'cases': {state: 1}, 'default': 0}

    def compare_out(self, name, out, where):
        if out not in ('match', 'mismatch'):
            raise LowerError('compiler', f'{where}: a compare has `.match` and `.mismatch`, not `.{out}`')
        c = self.compares[name]
        self.compare_used = c
        return f'words.{out}'

    def expr_signal(self, e, where):
        """One signal from an expression: a leaf, a pin qualifier, or a
        selection by machine state. None if it needs logic."""
        k = e['kind']
        if k == 'integer':
            v = _int(e, self.env)
            if v not in (0, 1):
                raise LowerError('hardware', f'{where}: constant {v} on a 1-bit signal')
            return v
        if k in ('name', 'field'):
            return self.leaf(e, where)
        q = self.qualifier(e)
        if q:
            return self.event(*q)
        if k == 'if':
            c = e['condition']
            if not (c['kind'] == 'field' and c['object'].get('kind') == 'name' and c['object']['name'] in self.machines):
                return None
            m, st = c['object']['name'], c['field']
            self.state_level(m, st, where)
            yes, no = self.expr_signal(e['yes'], where), self.expr_signal(e['no'], where)
            if yes is None or no is None:
                return None
            if isinstance(no, dict) and no.get('fsm') == m:
                cases = dict(no['cases'])
                cases[st] = yes
                return {'fsm': m, 'cases': cases, 'default': no.get('default', 0)}
            return {'fsm': m, 'cases': {st: yes}, 'default': no}
        return None

    # ---- events, registers (L4)
    def declared_event(self, name, where):
        if name not in self.declared:
            raise LowerError('compiler', f'{where}: unknown event {name}')
        if name in self.host_events:
            return name
        raise LowerError('compiler', f'{where}: event {name} is not fired by a host plan; '
                         'an event fired inside the fabric is not lowered yet (L3)')

    def leaf(self, e, where):
        """A single signal in a register expression."""
        if e['kind'] == 'name':
            n = e['name']
            if n in self.regs:
                return self.register(n)
            if n in self.declared:
                return self.declared_event(n, where)
            if n in self.pins:
                return self.event(n, 'level')
        if e['kind'] == 'field' and e['object'].get('kind') == 'name':
            n, out = e['object']['name'], e['field']
            if n in self.machines:
                return self.state_level(n, out, where)
            if n in self.compares:
                return self.compare_out(n, out, where)
            if n in self.pins and out in ('rise', 'fall'):
                return self.event(n, out)
            if n in self.res:
                return self.ref({'resolution': self.res[n]['resource_kind'], 'name': n, 'output': out}, where)
        return None

    def register(self, name):
        if name in self.reg_sig:
            if self.reg_sig[name] is None:
                raise LowerError('hardware', f'register {name}: a loop through registers')
            return self.reg_sig[name]
        self.reg_sig[name] = None
        r = self.regs[name]
        where = f'register {name}'
        init = _int(r['initial'], self.env) if r.get('initial') else 0
        if r.get('clock_enable'):
            raise LowerError('compiler', f'{where}: a clock enable is not lowered yet')
        v = r['value']
        # r = s / r = !s for an FSM register s: a mux of s's FSM.
        inv = v['kind'] == 'unary' and v['op'] == '!'
        base = v['value'] if inv else v
        if base['kind'] == 'name' and base['name'] in self.regs:
            s_sig = self.register(base['name'])
            m = self.signals[s_sig]
            cases = {st: (1 - c if inv else c) for st, c in m['cases'].items()}
            states = self.fsms[m['fsm']]['states']
            if cases[states[0]] != init:
                raise LowerError('hardware', f'{where}: init {init} differs from its source register at reset')
            self.signals[name] = {'fsm': m['fsm'], 'cases': cases}
            self.reg_sig[name] = name
            return name
        sc = self.set_clear(name, v)
        if sc is None:
            raise LowerError('hardware', f'{where}: needs logic (not a set/clear register or a copy of one)')
        a_e, clear = sc
        a = self.leaf(a_e, where)
        if a is None:
            raise LowerError('hardware', f'{where}: the set term needs logic')
        on, off = 'ON', 'OFF'
        if clear['kind'] == 'binary' and clear['op'] == '&&':
            x_e, y_e = clear['left'], clear['right']
            x, y = self.leaf(x_e, where), self.leaf(y_e, where)
            if self.monotone(y, a) and not self.monotone(x, a):
                x, y = y, x
            if x is None or y is None or not self.monotone(x, a):
                raise LowerError('hardware', f'{where}: a clear on two signals, neither held between sets; needs logic')
            states = [off, on, 'ARMED']
            on_rows = [[off, a, on], [on, a, on], [on, x, 'ARMED'], ['ARMED', a, on], ['ARMED', y, off]]
        else:
            b = self.leaf(clear, where)
            if b is None:
                raise LowerError('hardware', f'{where}: the clear term needs logic')
            states = [off, on]
            on_rows = [[off, a, on], [on, a, on], [on, b, off]]
        if init:
            states.remove(on)
            states.insert(0, on)
        f = f'{name}_fsm'
        self.fsms[f] = {'states': states, 'on': on_rows}
        self.signals[name] = {'fsm': f, 'cases': {st: int(st != off) for st in states}}
        self.reg_sig[name] = name
        return name

    @staticmethod
    def set_clear(name, v):
        """`A || (r && !C)` (either operand order) -> (A, C)."""
        if v['kind'] != 'binary' or v['op'] != '||':
            return None
        for a, hold in ((v['left'], v['right']), (v['right'], v['left'])):
            if hold['kind'] == 'binary' and hold['op'] == '&&':
                for me, nc in ((hold['left'], hold['right']), (hold['right'], hold['left'])):
                    if me['kind'] == 'name' and me['name'] == name and nc['kind'] == 'unary' and nc['op'] == '!':
                        return a, nc['value']
        return None

    def monotone(self, sig, set_sig):
        """True if `sig` can only rise between two occurrences of `set_sig`:
        a counter's at_least whose clear is `set_sig`."""
        if not isinstance(sig, str) or not sig.endswith('.at_least'):
            return False
        c = sig[:-len('.at_least')]
        clr = self.conns(c).get('counter.clear')
        return clr is not None and self.signal(clr) == set_sig

    def counter(self, name, f, ports, connect):
        spec = {'kind': 'counter'}
        if 'limit' in f:
            spec['limit'] = _int(f['limit'], self.env)
        elif name not in self.param_tiles:
            raise LowerError('compiler', f'{name}: no limit, and no host plan sets one')
        if 'streak' in f:
            e = f['streak']
            if e['kind'] != 'call' or e['callee'].get('name') not in ('ones', 'zeros', 'equal'):
                raise LowerError('compiler', f'{name}: streak {e["kind"]}')
            spec['mode'] = e['callee']['name']
        for port, sink in (('counter.count', 'step'), ('counter.clear', 'clear'), ('counter.streak', 'data')):
            if port in ports:
                connect[f'{name}.{sink}'] = self.signal(ports[port])
        extra = set(ports) - {'counter.count', 'counter.clear', 'counter.streak'}
        if extra:
            raise LowerError('compiler', f'{name}: inputs {sorted(extra)} not lowered yet')
        if 'limit' in spec and not 0 <= spec['limit'] < 256:
            raise LowerError('hardware', f'{name}: limit {spec["limit"]}; the counter has 8 bits')
        return spec

    # ---- exported functions (L3): host plans
    def host_expr(self, e, params):
        k = e['kind']
        if k in ('integer', 'quantity') or (k == 'name' and e['name'] in self.env):
            return _int(e, self.env)
        if k == 'name' and e['name'] in params:
            return {'arg': e['name']}
        if k == 'field' and e['field'] == 'len' and e['object'].get('kind') == 'name' and e['object']['name'] in params:
            return {'len': e['object']['name']}
        if k == 'binary' and e['op'] in ('+', '-', '*', '<<', '>>', '|', '&'):
            return {'op': e['op'], 'a': self.host_expr(e['left'], params), 'b': self.host_expr(e['right'], params)}
        raise LowerError('compiler', f'host expression {k} {e.get("op", "")}')

    def functions(self):
        self.tx_via, self.rx_via, self.param_tiles = [], [], set()
        for fn in self.items:
            if fn['kind'] != 'function':
                continue
            name = fn['name']
            if not fn.get('exported'):
                raise LowerError('compiler', f'function {name}: helper functions (inlining) are not lowered yet (L3)')
            params = {p: t for p, t in fn['params']}
            plan = {'args': {}, 'steps': [{'op': 'clear'}]}
            ret = fn.get('returns')
            rx = ret.get('via') if ret else None
            if ret and not rx:
                raise LowerError('compiler', f'{name}: a result not `via` a shifter is not lowered yet')
            if rx:
                self.rx_via.append(rx)
                plan['returns'] = 'writer'
            for p, t in params.items():
                if t.get('via'):
                    self.tx_via.append(t['via'])
                    plan['args'][p] = 'reader'
                    plan['steps'].append({'op': 'send', 'arg': p})
                else:
                    plan['args'][p] = 'value'
            fired, waited = False, set()
            for st in fn['body']:
                op = st['op']
                if op == 'assign':
                    t = st['target']
                    if not (t['kind'] == 'field' and t['object'].get('kind') == 'name'
                            and t['object']['name'] in self.res
                            and self.res[t['object']['name']]['resource_kind'] == 'counter' and t['field'] == 'limit'):
                        raise LowerError('compiler', f'{name}: assignment to {json.dumps(t)[:80]} not lowered yet')
                    if fired:
                        raise LowerError('compiler', f'{name}: a parameter write after the transaction started '
                                         '(the tile is not idle; a context or synchronized update is not lowered yet)')
                    tile = t['object']['name']
                    self.param_tiles.add(tile)
                    plan['steps'].append({'op': 'param', 'tile': tile, 'reg': 'limit',
                                          'value': self.host_expr(st['value'], params)})
                elif op == 'fire':
                    e = st['event']
                    if e not in self.declared:
                        raise LowerError('compiler', f'{name}: fire of unknown event {e}')
                    if e not in self.host_events:
                        self.host_events.append(e)
                    fired = True
                    plan['steps'].append({'op': 'fire', 'event': e})
                elif op == 'wait' and not st.get('next'):
                    sig = self.leaf(st['signal'], f'{name}: wait')
                    if not isinstance(sig, str) or '.' not in sig:
                        raise LowerError('compiler', f'{name}: wait on {sig}: only a tile output has a host flag')
                    if sig in waited:
                        raise LowerError('compiler', f'{name}: a second wait on {sig} in one call (a sticky flag '
                                         'does not count; an FSM or counter is not lowered yet)')
                    waited.add(sig)
                    plan['steps'].append({'op': 'wait', 'signal': sig})
                elif op == 'repeat' and len(st['body']) == 1 and st['body'][0]['op'] == 'wait':
                    w = st['body'][0]['signal']
                    if not (w['kind'] == 'field' and w['field'] == 'word' and w['object'].get('name') == rx):
                        raise LowerError('compiler', f'{name}: repeat of a wait other than the result shifter\'s word')
                    plan['steps'].append({'op': 'wait_bytes', 'count': self.host_expr(st['count'], params)})
                else:
                    raise LowerError('compiler', f'{name}: `{op}` in a function is not lowered yet (L3)')
            if rx:
                plan['steps'].append({'op': 'reply'})
            self.plans[name] = plan

    # ---- resources
    def lower(self):
        unsupported = [i for i in self.items if i['kind'] in ('flags', 'context', 'context_selector')]
        tiles, connect, drivers = {}, {}, {}
        self.tiles, self.connect = tiles, connect
        self.functions()
        for m in self.machines:
            self.machine(m)
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
            elif kind == 'counter':
                tiles[name] = self.counter(name, fields, ports, connect)
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
        for e in self.declared:
            if e not in self.host_events and e not in self.fired_in_fabric:
                raise LowerError('compiler', f'event {e} is never fired')
        events = {n: e for n, e in self.events.values()}
        events.update({e: {'host': True} for e in self.host_events})
        design = {'name': self.name, 'about': f'Lowered from {self.ir.get("source", "source")} by fabric_lower.py.',
                  'pins': {p: self.pads[p] for p in self.pins},
                  'events': events,
                  'drivers': drivers, 'tiles': tiles, 'connect': connect}
        if self.fsms:
            design['fsms'] = self.fsms
            design['signals'] = self.signals
        if self.plans:
            design['functions'] = self.plans
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
        msb = False
        for v in fr['values']:
            if v['kind'] == 'integer':
                (post if seen else pre).append(int(v['value']))
            elif v['kind'] == 'call' and v['callee'].get('name') == 'data':
                seen, length = True, _int(v['args'][0]['value'], self.env)
                for arg in v['args'][1:]:
                    if arg['value'].get('kind') == 'name' and arg['value']['name'] == 'msb_first':
                        msb = True
                    else:
                        raise LowerError('compiler', f'{name}: data() argument {json.dumps(arg)[:60]}')
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
        if 'msb_first' in f or msb:
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
        gate = None
        for s in streams:
            stages = s.get('stages') or []
            if [st['operation'] for st in stages] == ['gate'] and s in rx:
                f = {x['name']: x['value'] for x in stages[0]['fields']}
                if set(f) != {'open'}:
                    raise LowerError('compiler', f'stream {s["name"]}: gate takes `open: <level>`')
                gate = (s, self.expr_signal(f['open'], f'stream {s["name"]} gate'))
                if gate[1] is None:
                    raise LowerError('hardware', f'stream {s["name"]}: the gate level needs logic')
            elif stages:
                raise LowerError('compiler', f'stream {s["name"]}: stages not lowered yet (L5)')
            if s not in tx and s not in rx:
                raise LowerError('compiler', f'stream {s["name"]}: neither end is the host')
        tx_sh = [s['to']['name'] for s in tx] + [v for v in dict.fromkeys(self.tx_via)]
        rx_sh = [s['from']['name'] for s in rx] + [v for v in dict.fromkeys(self.rx_via)]
        if len(tx_sh) > 1:
            raise LowerError('hardware', f'{len(tx_sh)} transmit streams; the word network has one reader into a shifter')
        if len(rx_sh) > 1 and rx_sh[1] in self.rx_via:
            raise LowerError('hardware', 'a function result and another receive stream; one writer')
        words = {}
        for k, sh in enumerate(rx_sh):
            tiles[sh].setdefault('io', {})['auto_push'] = True
            if k == 0:                       # the writer: into SRAM
                tiles[sh]['rx_port'] = True
                words['writer'] = 'rx'
                if gate and gate[0]['from'].get('name') == sh:
                    words['gate'], words['writer'] = 'rx', 'gate'
                    self.connect['words.gate'] = gate[1]
            elif gate and gate[0]['from'].get('name') == sh:
                raise LowerError('hardware', f'stream {gate[0]["name"]}: a gate only on the stream into the writer')
        if tx_sh:
            sh = tx_sh[0]
            tiles[sh]['io']['auto_pull'] = True
            tiles[sh]['tx_port'] = True
            words['tx'] = 'reader'
        for name, c in self.compares.items():
            if not rx_sh or c['shifter'] != rx_sh[0]:
                raise LowerError('hardware', f'compare {name}: the fabric compares words on the receive channel '
                                 f'into the writer; {c["shifter"]} is not that shifter')
            if len(self.compares) > 1:
                raise LowerError('hardware', f'{len(self.compares)} compares; the word network has one')
            f = {x['name']: x['value'] for x in c['fields']}
            if set(f) - {'value', 'mask'} or 'value' not in f:
                raise LowerError('compiler', f'compare {name}: fields are value and mask')
            value, mask = _int(f['value'], self.env), _int(f['mask'], self.env) if 'mask' in f else 0xFF
            if not (0 <= value < 256 and 0 <= mask < 256):
                raise LowerError('hardware', f'compare {name}: value and mask are bytes')
            words.update(tap='rx', cmp_value=value, cmp_mask=mask)
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
    lw.limits = {'states': contract['layout']['contexts'], 'transitions': contract['layout']['transition_rows']}
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
