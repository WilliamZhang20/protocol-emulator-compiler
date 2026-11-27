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

from fabric_logic import LogicError, flag_writes, make_lut, mux_key, tiles_of, unkey


class LowerError(ValueError):
    def __init__(self, cls, msg):
        super().__init__(f'{cls}: {msg}')
        self.cls = cls


def _strip(e):
    """An IR expression without source spans (to compare two by value)."""
    if isinstance(e, dict):
        return {k: _strip(v) for k, v in e.items() if k != 'source_span'}
    if isinstance(e, list):
        return [_strip(v) for v in e]
    return e


def _field_word(reg, fields):
    """A register word from named fields through the contract's field map."""
    word = 0
    for name, x in fields.items():
        if name == 'parity' and isinstance(x, str):
            x = reg['parity'][x]
        hi, lo = reg['fields'][name]
        if not 0 <= int(x) < (1 << (hi - lo + 1)):
            raise LowerError('hardware', f'register field {name} = {x} does not fit [{hi}:{lo}]')
        word |= int(x) << lo
    return word


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
        self.env, self.lists = {}, {}
        for i in self.items:
            if i['kind'] == 'constant' and i['value']['kind'] in ('integer', 'quantity'):
                self.env[i['name']] = _int(i['value'], {})
            elif i['kind'] == 'constant' and i['value']['kind'] == 'list':
                self.lists[i['name']] = i['value']
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
        self.bidirectional = set()
        self.compares = {i['name']: i for i in self.items if i['kind'] == 'compare'}
        self.protocols = {p['name']: p for p in ir.get('protocols', [])}
        self.proto_out, self.synth_tiles = {}, {}
        # Logic tiles (fabric_logic.py): LUTs by name, registers lowered to
        # LUTs, host flags and their start values, combinational lags.
        self.flag_init = {}
        for i in self.items:
            if i['kind'] == 'flags':
                for n, v in i['flags']:
                    self.flag_init[n] = _int(v, self.env) if v else 0
        self.luts, self.lut_regs, self.lags, self.comb = {}, set(), [], {}
        self.logic = None          # the contract's logic kind, if it has logic cells
        self.prefer_lut = False    # registers as LUTs even where an FSM would do
        self.timer_cells = None    # the contract's timer cells (small timers become LUTs past them)
        self.sig_alias = {}        # tile output -> LUT, for tiles lowered to LUTs
        self.seq_flags, self.seq_events, self.drive_flag = {}, {}, {}
        self.armed = {}            # (signal, event) -> (arm flag, fire LUT)

    # ---- signals
    def event(self, pin, edge, qual=None):
        """The I/O output for a pin's level/rise/fall, optionally qualified."""
        if pin not in self.pins or self.pins[pin]['mode'] not in ('in', 'inout', 'open_drain'):
            if pin in self.pins and self.pins[pin]['mode'] == 'out' and pin not in self.bidirectional:
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
        if self.logic:
            return self.comb_lut(expr, where)
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
            if out not in ('ok', 'out'):
                raise LowerError('compiler', f'{where}: crc output {out}')
            return f'{name}.{out}'
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
        if kind == 'protocol':
            return self.protocol(name, out, where)
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

    def protocol(self, name, out, where):
        """A protocol's action pulse, from its minimized machine (the
        frontend's reachability and state classes). The mesh has no
        Mealy-action tile, so a protocol lowers only where its minimized
        shape is one the mesh has hardware for:
          - a counting chain (each state advances on the input, holds
            without it, the last state absorbs): action k on step i is a
            counter tile counting the input, limit i+1, `reached` (the
            saturating counter pulses once, as the absorbing chain does);
          - a level tracker (two states following one input, which is low
            at reset): an action on the low->high step is the input's rise,
            on high->low its fall.
        Anything else is refused: an action of a general machine needs a
        pulse per transition, which the mesh's machines do not output."""
        if (name, out) in self.proto_out:
            return self.proto_out[(name, out)]
        if not (out or '').startswith('action') or not out[6:].isdigit():
            raise LowerError('compiler', f'{where}: protocol {name} has actions, not `.{out}`')
        action = int(out[6:])
        p = self.protocols.get(name)
        if p is None:
            raise LowerError('compiler', f'{where}: protocol {name} is not in the model IR')
        if len(p['inputs']) != 1:
            raise LowerError('hardware', f'protocol {name}: transitions on {len(p["inputs"])} inputs need logic '
                             '(the mesh\'s machines take one signal per transition)')
        reach = p['analysis']['reachable_states']
        cls = {s: i for i, c in enumerate(p['analysis']['equivalence_classes']) for s in c}
        rows = {}
        for t in p['transitions']:
            if t['from'] not in reach:
                continue
            key = (cls[t['from']], t['inputs'][0])
            rows[key] = (cls[t['to']], t['action'])
        n = len(p['analysis']['equivalence_classes'])

        def step(c, v):            # unspecified rows hold silently
            return rows.get((c, v), (c, None))
        start = cls[p['initial_state']]
        inp = p['inputs'][0]
        if inp['kind'] != 'resource':
            raise LowerError('hardware', f'protocol {name}: a constant input')
        src = self.ref({'resolution': self.res[inp['name']]['resource_kind'] if inp['name'] in self.res
                        else 'pin', 'name': inp['name'], 'output': inp['output']}, where)
        # The chain order from the reset class.
        order, c = [start], start
        while len(order) <= n:
            nxt = step(c, True)[0]
            if nxt == c or nxt in order:
                break
            order.append(nxt)
            c = nxt
        chain = len(order) == n and all(step(c, False) == (c, None) for c in order) and \
            step(order[-1], True) == (order[-1], None) and \
            all(step(order[i], True)[0] == order[i + 1] for i in range(n - 1))
        if chain:
            at = [i for i in range(n - 1) if step(order[i], True)[1] == action]
            if len(at) != 1:
                raise LowerError('compiler', f'{where}: action {action} of protocol {name} is not on one step')
            tile = f'_{name}_{out}'
            self.synth_tiles[tile] = {'kind': 'counter', 'limit': at[0] + 1}
            self.connect[f'{tile}.step'] = src
            sig = f'{tile}.reached'
        elif n == 2 and step(start, False) == (start, None) and step(1 - start, True) == (1 - start, None) \
                and step(start, True)[0] == 1 - start and step(1 - start, False)[0] == start:
            if inp['name'] not in self.res or self.res[inp['name']]['resource_kind'] != 'timer' \
                    or inp['output'] != 'out':
                raise LowerError('compiler', f'protocol {name}: a level tracker of {inp["name"]}.{inp["output"]} '
                                 '(only a timer level, low at reset, is lowered)')
            edges = {step(start, True)[1]: 'rise', step(1 - start, False)[1]: 'fall'}
            if action not in edges:
                raise LowerError('compiler', f'{where}: protocol {name} never fires action {action}')
            sig = f'{inp["name"]}.{edges[action]}'
        else:
            raise LowerError('hardware', f'protocol {name}: {n} states after minimization, neither a counting '
                             'chain nor a level tracker; its actions need a pulse per transition, which the '
                             'mesh\'s machines do not output')
        self.proto_out[(name, out)] = sig
        return sig

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
        if name in self.seq_events:
            return self.seq_events[name]
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
            if n in self.protocols:
                return self.protocol(n, out, where)
            if n in self.pins and out in ('rise', 'fall'):
                return self.event(n, out)
            if n in self.res:
                return self.ref({'resolution': self.res[n]['resource_kind'], 'name': n, 'output': out}, where)
        return None

    # ---- logic tiles
    def lkey(self, e, where, me=None):
        """A LUT leaf key for a non-operator expression."""
        if e['kind'] == 'name':
            n = e['name']
            if me is not None and n == me:
                return f'lut:{me}'
            if n in self.seq_flags:
                return self.seq_flags[n]
            if n in self.flag_init:
                return f'flag:{n}'
            if n in self.env:
                v = _int(e, self.env)
                if v not in (0, 1):
                    raise LowerError('hardware', f'{where}: constant {v} on a 1-bit signal')
                return v
        v = self.leaf(e, where)
        if v is None:
            raise LowerError('hardware', f'{where}: {e["kind"]} {e.get("name", "")} is not a 1-bit signal')
        return mux_key(v) if isinstance(v, dict) else v

    def make_lut(self, name, expr, where, **kw):
        me = None if name.startswith('~') else name          # a register may read itself
        try:
            lut = make_lut(name, expr, lambda e: self.lkey(e, where, me), flag_init=self.flag_init,
                           inputs=4, **kw)
        except LogicError as e:
            raise LowerError('hardware', f'{where}: {e}')
        self.luts[name] = lut
        return f'lut:{name}'

    def comb_lut(self, expr, where):
        """Combinational source logic: a LUT one clock late."""
        key = json.dumps(_strip(expr), sort_keys=True)
        if key not in self.comb:
            name = f'~c{len(self.comb)}'
            self.comb[key] = self.make_lut(name, expr, where, lag=1)
            self.lags.append(where)
        return self.comb[key]

    def _snapshot(self):
        return {k: dict(getattr(self, k)) for k in ('fsms', 'signals', 'reg_sig', 'synth_tiles', 'proto_out',
                                                     'events', 'luts', 'comb') if hasattr(self, k)} | \
            {'connect': dict(getattr(self, 'connect', {})), 'lut_regs': set(self.lut_regs), 'lags': list(self.lags)}

    def _restore(self, snap):
        for k, v in snap.items():
            if k == 'connect' and hasattr(self, 'connect'):
                self.connect.clear()
                self.connect.update(v)
            else:
                setattr(self, k, v)

    def register(self, name):
        """A register's signal: an FSM on contexted switches where the
        register has that shape (below), else a LUT of a logic tile."""
        if name in self.lut_regs:
            return f'lut:{name}'
        if name in self.reg_sig or not self.logic:
            return self._register_fsm(name)
        r = self.regs[name]
        if not r.get('clock_enable') and not self.prefer_lut:
            snap = self._snapshot()
            try:
                return self._register_fsm(name)
            except LowerError as e:
                if e.cls != 'hardware':
                    raise
                self._restore(snap)
        return self._register_lut(name)

    def _register_lut(self, name):
        r = self.regs[name]
        where = f'register {name}'
        self.lut_regs.add(name)
        init = _int(r['initial'], self.env) if r.get('initial') else 0
        en = None
        if r.get('clock_enable'):
            en = self.lkey(r['clock_enable'], where, name)
            if isinstance(en, int):
                if not en:
                    raise LowerError('hardware', f'{where}: enabled by constant 0')
                en = None
        return self.make_lut(name, r['value'], where, enable=en, init=init)

    def _register_fsm(self, name):
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
                c = ports[port]
                e = c['expression']
                if port == 'counter.streak' and e['kind'] == 'call' and len(e['args']) == 1:
                    c = dict(c, expression=e['args'][0]['value'])     # ones(x): the mode is the call, the data x
                connect[f'{name}.{sink}'] = self.signal(c)
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

    # ---- wire-timed function segments: a sequencer in logic tiles
    def raw_lut(self, name, expr, where, init=0):
        try:
            lut = make_lut(name, expr, lambda e: e['key'] if e['kind'] == 'key' else self.lkey(e, where),
                           flag_init=self.flag_init, init=init, inputs=4)
        except LogicError as e:
            raise LowerError('hardware', f'{where}: {e}')
        self.luts[name] = lut
        return f'lut:{name}'

    def wire_segment(self, body, sending):
        """The part of a call that must run in the fabric: from the first
        wait while a transaction is in flight (a shifter fed by the call and
        not yet done) to the last flag change or fire while one is. Such a
        step acts at the transaction's own pace (a bit or a word), which a
        host does not keep. Returns (first, last) statement indices or None."""
        inflight, first, last = set(sending), None, None

        def ends(sig):
            if sig['kind'] != 'field' or sig['object'].get('kind') != 'name':
                return set()
            o, f = sig['object']['name'], sig['field']
            if f == 'done' and o in inflight:
                return {o}
            if o in self.res and self.res[o]['resource_kind'] == 'timer':
                # a timer started by a shifter's done: its output comes after it
                out = set()
                for c in self.inputs:
                    if c['consumer'] == o and c['port'] in ('timer.start', 'timer.run'):
                        for r in c['references']:
                            if r['name'] in inflight and r['output'] == 'done':
                                out.add(r['name'])
                return out
            return set()
        for i, st in enumerate(body):
            if st['op'] == 'evaluate':
                e = st['expression']
                c = e.get('callee', {})
                if c.get('kind') == 'field' and c.get('field') == 'send':
                    inflight.add(c['object'].get('name'))
                if c.get('kind') == 'name' and c.get('name') in ('set_flags', 'clear_flags') and inflight \
                        and first is not None:
                    last = i
            elif st['op'] == 'wait':
                if inflight and first is None:
                    first = i
                inflight -= ends(st['signal'])
            elif st['op'] == 'fire' and inflight and first is not None:
                last = i
        return (first, last) if last is not None else None

    def sequencer(self, fn, stmts):
        """A wire-timed segment as a one-hot state machine of LUTs: state k
        waits for the k-th wait's signal c_k, q_k' = (q_{k-1} && c_{k-1}) ||
        (q_k && !c_k); the last wait returns to state 0, which waits for the
        next call. A flag is a register of the states it holds in, a fire a
        one-clock pulse on its transition (both one clock after the state,
        as a controller acts a few clocks after its wait). Returns the signal
        of the segment's end, for the host to wait on."""
        K = lambda k: {'kind': 'key', 'key': k}
        AND = lambda a, b: {'kind': 'binary', 'op': '&&', 'left': a, 'right': b}
        OR = lambda a, b: {'kind': 'binary', 'op': '||', 'left': a, 'right': b}
        NOT = lambda a: {'kind': 'unary', 'op': '!', 'value': a}
        waits, values, fires, cur = [], [], {}, dict(self.flag_init)
        where = f'{fn}: wire-timed segment'
        for st in stmts:
            if st['op'] == 'wait':
                values.append(dict(cur))
                waits.append(st['signal'])
            elif st['op'] == 'fire':
                if not waits:
                    raise LowerError('compiler', f'{where}: a fire before its first wait')
                fires.setdefault(len(waits) - 1, []).append(st['event'])
            elif st['op'] == 'evaluate':
                e = st['expression']
                on = [a['value'].get('name') for a in e['args']]
                cur = {f: int(f in on) for f in self.flag_init if not f.startswith('_')}
                cur.update({f: v for f, v in self.flag_init.items() if f.startswith('_')})
            else:
                raise LowerError('compiler', f'{where}: `{st["op"]}` in a wire-timed segment is not lowered yet')
        if cur != values[0]:
            raise LowerError('compiler', f'{where}: the flags at its end differ from its start')
        n = len(waits)
        tag = f'~{fn}'
        q = [f'lut:{tag}_s{k}' for k in range(n)]
        for f in self.flag_init:
            if len({v[f] for v in values}) > 1:
                if f in self.seq_flags:
                    raise LowerError('compiler', f'{where}: flag {f} changed by two sequencers')
                self.seq_flags[f] = f'lut:{tag}_flag_{f}'
        last_fire = fires.get(n - 1)
        for k, evs in fires.items():
            for e in evs:
                self.seq_events[e] = f'lut:{tag}_fire{k}'
                self.fired_in_fabric.append(e)
        c = [self.lkey(w, where) for w in waits]
        for k in range(n):
            prev = (k - 1) % n
            if n == 1:
                expr = {'kind': 'integer', 'value': '1'}
            else:
                expr = OR(AND(K(q[prev]), K(c[prev])), AND(K(q[k]), NOT(K(c[k]))))
            self.raw_lut(f'{tag}_s{k}', expr, where, init=int(k == 0))
        for f, lut in self.seq_flags.items():
            if not lut.startswith(f'lut:{tag}_'):
                continue
            on = [k for k in range(n) if values[k][f]]
            off = [k for k in range(n) if not values[k][f]]
            terms, inv = (on, False) if len(on) <= len(off) else (off, True)
            expr = K(q[terms[0]])
            for k in terms[1:]:
                expr = OR(expr, K(q[k]))
            self.raw_lut(lut[4:], NOT(expr) if inv else expr, where, init=values[0][f])
        for k in fires:
            self.raw_lut(f'{tag}_fire{k}', AND(K(q[k]), K(c[k])), where)
        if last_fire is None:
            self.raw_lut(f'{tag}_fire{n - 1}', AND(K(q[n - 1]), K(c[n - 1])), where)
        return f'lut:{tag}_fire{n - 1}'

    def free_running(self, sig):
        """A timer output the host does not cause (its run and start are
        constants): a time reference the fabric keeps on its own."""
        if sig['kind'] != 'field' or sig['object'].get('kind') != 'name':
            return False
        o = sig['object']['name']
        if o not in self.res or self.res[o]['resource_kind'] != 'timer':
            return False
        return all(not c['references'] for c in self.inputs
                   if c['consumer'] == o and c['port'] in ('timer.start', 'timer.run'))

    def armed_fire(self, fn, wait, event):
        """`wait next T; fire E` with T a free-running timer: E is timed by
        T, so it fires in the fabric on T's next output once the host arms
        it (a host flag): fire' = arm && T && !done, done' = arm && (done ||
        T), one pulse per arming, one clock after T. The host arms it after
        its previous step (well inside T's period), waits for the fire and
        disarms it: the host's latency moves no wire edge."""
        K = lambda k: {'kind': 'key', 'key': k}
        AND = lambda a, b: {'kind': 'binary', 'op': '&&', 'left': a, 'right': b}
        where = f'{fn}: fire {event} after wait next'
        tick = self.lkey(wait, where)
        key = (json.dumps(tick, sort_keys=True), event)
        if key not in self.armed:
            if event in self.seq_events or event in self.host_events:
                raise LowerError('compiler', f'{where}: {event} is also fired elsewhere (by the host or another '
                                 'reference); one event has one source')
            n = len(self.armed)
            arm, fire, done = f'_arm{n}', f'_armfire{n}', f'_armdone{n}'
            self.flag_init[arm] = 0
            self.raw_lut(fire, AND(AND(K(f'flag:{arm}'), K(tick)), {'kind': 'unary', 'op': '!', 'value': K(f'lut:{done}')}), where)
            self.raw_lut(done, AND(K(f'flag:{arm}'), {'kind': 'binary', 'op': '||', 'left': K(f'lut:{done}'), 'right': K(tick)}), where)
            self.armed[key] = (arm, f'lut:{fire}')
            self.seq_events[event] = f'lut:{fire}'
            self.fired_in_fabric.append(event)
        arm, fire = self.armed[key]
        return [{'op': 'clear', 'keep': True}, {'op': 'flags', 'values': {arm: 1}},
                {'op': 'wait', 'signal': fire, 'armed': True}, {'op': 'flags', 'values': {arm: 0}}]

    def drive_flags(self):
        """Pins a call drives and releases (both in one call): their pad
        enable is a host flag in a logic tile (a host step at a quiescent
        point; the mode machine holds one setting per call). Pins switched
        by the same statements share one flag."""
        sig = {}
        for fn in self.items:
            if fn['kind'] != 'function':
                continue
            group, run, seen = 0, False, {}
            for st in fn['body']:
                e = st.get('expression', {}) if st['op'] == 'evaluate' else {}
                c = e.get('callee', {})
                if e.get('kind') == 'call' and c.get('kind') == 'field' and c['field'] in ('drive', 'release') \
                        and c['object'].get('name') in self.pins:
                    if not run:
                        group += 1
                    run = True
                    seen.setdefault(c['object']['name'], []).append((group, c['field']))
                else:
                    run = False
            for pin, ev in seen.items():
                if {f for _, f in ev} == {'drive', 'release'}:
                    sig.setdefault(pin, []).append((fn['name'], tuple(ev)))
        names = {}
        for pin, s in sorted(sig.items()):
            key = json.dumps(s)
            names.setdefault(key, f'_drive{len(names)}')
            self.drive_flag[pin] = names[key]
            self.flag_init[names[key]] = 0

    def functions(self):
        self.tx_via, self.rx_via, self.param_tiles = [], [], set()
        if self.logic:
            self.drive_flags()
        self.fn_modes, self.fn_runs, self.loaded = {}, {}, set()
        # Output pins a call releases and drives again are bidirectional: the
        # pad's level (driven or not) is an input, as on every io cell.
        self.bidirectional = {e['callee']['object']['name'] for fn in self.items if fn['kind'] == 'function'
                              for st in fn['body'] if st['op'] == 'evaluate'
                              for e in [st['expression']] if e['kind'] == 'call'
                              and e['callee'].get('kind') == 'field' and e['callee']['field'] in ('drive', 'release')
                              and e['callee']['object'].get('kind') == 'name'}
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
            replies = [st for st in fn['body'] if st['op'] == 'reply']
            mask = None
            if ret and not rx:
                # `reply X.received` (optionally `& mask`): the result is the
                # word shifter X receives, through the writer.
                rx, mask = self.reply_shifter(name, replies)
            elif replies:
                raise LowerError('compiler', f'{name}: `reply` beside a `via` result')
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
            fired, waited, changed, mode, waited_src = False, set(), set(), {}, set()

            def before_fire(what):
                if fired:
                    raise LowerError('compiler', f'{name}: {what} after the transaction started: a host step is '
                                     'safe only before the first `fire` (no transaction in flight); during a '
                                     'transaction it must run on the fabric (an FSM), not lowered yet')
            def rx_words(st):
                w = st['body'][0]['signal'] if len(st['body']) == 1 and st['body'][0]['op'] == 'wait' else None
                return w is not None and w['kind'] == 'field' and w['field'] == 'word' and w['object'].get('name') == rx

            def expand(body):
                """`repeat N { ... }` with a constant N, unrolled (a host loop
                of host steps); `repeat N { wait S.word }` on the result
                shifter stays a byte count."""
                out = []
                for st in body:
                    if st['op'] == 'repeat' and not rx_words(st):
                        try:
                            n = _int(st['count'], self.env)
                        except LowerError:
                            raise LowerError('compiler', f'{name}: a repeat count known only at run time '
                                             '(other than received words) is not lowered yet')
                        if not 0 <= n <= 1024:
                            raise LowerError('compiler', f'{name}: repeat {n}')
                        for _ in range(n):
                            out += expand(st['body'])
                    else:
                        out.append(st)
                return out
            body = expand(fn['body'])
            seg = self.wire_segment(body, [t['via'] for t in params.values() if t.get('via')]) \
                if self.logic else None
            skip = set()
            for idx, st in enumerate(body):
                op = st['op']
                if idx in skip:
                    continue
                if self.logic and op == 'wait' and st.get('next') and idx + 1 < len(body) and \
                        body[idx + 1]['op'] == 'fire' and self.free_running(st['signal']) and \
                        not (seg and seg[0] <= idx <= seg[1]):
                    plan['steps'] += self.armed_fire(name, st['signal'], body[idx + 1]['event'])
                    fired = True
                    skip.add(idx + 1)
                    continue
                if seg and idx == seg[0]:
                    done = self.sequencer(name, body[seg[0]:seg[1] + 1])
                    plan['steps'].append({'op': 'wait', 'signal': done})
                    continue
                if seg and seg[0] < idx <= seg[1]:
                    continue
                if op == 'reply':
                    continue
                if op == 'set':
                    t = st['target']
                    x = t.get('name') if t.get('kind') == 'name' else None
                    if x not in self.res or self.res[x]['resource_kind'] != 'shifter':
                        raise LowerError('compiler', f'{name}: `set` on {x or t.get("kind")} not lowered yet (shifters)')
                    before_fire(f'`{x}.set`')
                    fields = {f['name']: f['value'] for f in st['fields']}
                    other = set(fields) - {'frame', 'idle', 'msb_first', 'sample'}
                    if other:
                        raise LowerError('compiler', f'{name}: `{x}.set` of {sorted(other)} not lowered yet')
                    if 'sample' in fields:
                        v = fields.pop('sample')
                        if not (v['kind'] == 'binary' and v['op'] == 'at'):
                            raise LowerError('compiler', f'{name}: `{x}.set` sample must be `<data> at <event>`')
                        mode[('sample', x)] = v          # a route: selected by the mode machine
                    if fields:                           # tile registers: a parameter write into the idle tile
                        declared = {f['name']: f['value'] for f in self.res[x]['fields']}
                        word = _field_word(self.registers['shifter']['frame'],
                                           self.frame(x, {**declared, **fields}))
                        plan['steps'].append({'op': 'param', 'tile': x, 'reg': 'frame', 'value': word})
                        self.param_tiles.add(x)
                    changed.add(x)
                    continue
                if op == 'evaluate':
                    e = st['expression']
                    c = e.get('callee', {})
                    obj = c.get('object', {}).get('name') if e['kind'] == 'call' and c.get('kind') == 'field' else None
                    meth = c.get('field')
                    if e['kind'] == 'call' and c.get('kind') == 'name' and c.get('name') in ('set_flags', 'clear_flags'):
                        # The complete flag set (README, Setting flags): a host write
                        # of every logic tile's flag bits, resolved after packing.
                        if not self.logic:
                            raise LowerError('hardware', f'{name}: flags need logic cells; this fabric has none')
                        on = [a['value'].get('name') for a in e['args']]
                        if any(f not in self.flag_init for f in on):
                            raise LowerError('compiler', f'{name}: {c["name"]} of an undeclared flag')
                        plan['steps'].append({'op': 'flags', 'values': {f: int(f in on) for f in self.flag_init
                                                                        if not f.startswith('_')}})
                        continue
                    if obj in self.drive_flag and meth in ('drive', 'release') and not e['args']:
                        v = {self.drive_flag[obj]: int(meth == 'drive')}
                        last = plan['steps'][-1]
                        if last['op'] == 'flags' and last.get('drive'):
                            last['values'].update(v)
                        else:
                            plan['steps'].append({'op': 'flags', 'values': v, 'drive': True})
                        continue
                    if obj in self.pins and meth in ('drive', 'release') and not e['args']:
                        before_fire(f'`{obj}.{meth}()`')
                        mode[('drive', obj)] = int(meth == 'drive')
                        continue
                    if obj in self.res and self.res[obj]['resource_kind'] == 'shifter' and meth == 'load' \
                            and len(e['args']) == 1:
                        # A loaded value is a tile-local write (the shifter's holding
                        # register), not a stream: the reader stays free for streams.
                        before_fire(f'`{obj}.load`')
                        self.loaded.add(obj)
                        self.param_tiles.add(obj)
                        plan['steps'].append({'op': 'param', 'tile': obj, 'reg': 'load',
                                              'value': self.host_expr(e['args'][0]['value'], params)})
                        changed.add(obj)
                        continue
                    if obj in self.res and self.res[obj]['resource_kind'] == 'shifter' and meth == 'send' \
                            and len(e['args']) == 1:
                        # Stored bytes: into SRAM by the host, streamed by the reader
                        # (the same path as a `via` argument, with constant data).
                        self.tx_via.append(obj)
                        plan['steps'].append({'op': 'send', 'bytes': self.const_bytes(e['args'][0]['value'],
                                                                                    f'{name}: {obj}.send')})
                        continue
                    what = f'{obj}.{meth}()' if obj else (c.get('name') or e['kind'])
                    raise LowerError('compiler', f'{name}: `{what}` in a function is not lowered yet (L3)')
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
                    if e in self.seq_events:
                        raise LowerError('compiler', f'{name}: the host fires {e}, which the fabric also fires')
                    if e not in self.host_events:
                        self.host_events.append(e)
                    fired = True
                    plan['steps'].append({'op': 'fire', 'event': e})
                elif op == 'wait':
                    w = st['signal']
                    if w['kind'] == 'field' and w['object'].get('kind') == 'name':
                        waited_src.add(f"{w['object']['name']}.{w['field']}")
                    sig = self.leaf(st['signal'], f'{name}: wait')
                    if not isinstance(sig, str) or ('.' not in sig and not sig.startswith('lut:')):
                        raise LowerError('compiler', f'{name}: wait on {sig}: only a tile output has a host flag')
                    if st.get('next'):
                        # `wait next X`: only an X after this point (the
                        # controller forgets remembered events first).
                        plan['steps'].append({'op': 'clear'})
                    waited.add(sig)
                    plan['steps'].append({'op': 'wait', 'signal': sig})
                elif op == 'repeat' and len(st['body']) == 1 and st['body'][0]['op'] == 'wait':
                    w = st['body'][0]['signal']
                    if not (w['kind'] == 'field' and w['field'] == 'word' and w['object'].get('name') == rx):
                        raise LowerError('compiler', f'{name}: repeat of a wait other than the result shifter\'s word')
                    plan['steps'].append({'op': 'wait_bytes', 'count': self.host_expr(st['count'], params)})
                else:
                    raise LowerError('compiler', f'{name}: `{op}` in a function is not lowered yet (L3)')
            # `via` arguments are sent once the call's leading setup (pin
            # drive and flag writes) is done, so a transaction starts on
            # driven pins, as in the source's statement order.
            sends = [st for st in plan['steps'] if st['op'] == 'send' and 'arg' in st]
            rest = [st for st in plan['steps'] if not (st['op'] == 'send' and 'arg' in st)]
            k = 1 if rest and rest[0]['op'] == 'clear' else 0
            while k < len(rest) and rest[k]['op'] == 'flags':
                k += 1
            plan['steps'] = rest[:k] + sends + rest[k:]
            # A wait consumes its event (the controller's WEV): a later wait on
            # the same output waits for a new one. The host's flags are
            # sticky and cleared together, so the flags are cleared right
            # after a wait whose output is waited on again (a quiescent
            # point: the host has just seen the fabric reach it).
            steps = plan['steps']
            out = []
            for i, st in enumerate(steps):
                out.append(st)
                if st['op'] == 'wait':
                    # (not needed when the next wait on it is a `wait next`,
                    # which clears just before it; a clear between a wait and
                    # the step after it would also shift that step by the
                    # clear's clocks, unevenly between otherwise equal steps)
                    later = next((x for x in steps[i + 1:] if x['op'] in ('wait', 'clear')), None)
                    again = next((j for j in range(i + 1, len(steps))
                                  if steps[j]['op'] == 'wait' and steps[j]['signal'] == st['signal']), None)
                    if again is not None and steps[again - 1]['op'] != 'clear' and not st.get('armed') and \
                            not (later and later['op'] == 'clear'):
                        out.append({'op': 'clear'})
            plan['steps'] = out
            for x in sorted(changed):
                if f'{x}.done' not in waited_src:
                    raise LowerError('compiler', f'{name}: the host changes {x} before the transaction but the call '
                                     f'does not wait for `{x}.done`, so the next call could change it mid-word')
            # A call that waits before it fires anything waits for an event it
            # does not cause (a free-running or one-shot fabric output): its
            # sticky flags are not cleared at the call's start, so an event that
            # came before the host got to the call still counts (the host's
            # latency is not the fabric's to hide).
            first = next((st['op'] for st in plan['steps'] if st['op'] in ('fire', 'wait', 'send')), None)
            if first == 'wait' and not mode and plan['steps'][0]['op'] == 'clear' and not plan['steps'][0].get('keep'):
                plan['steps'] = plan['steps'][1:]            # only the call-start clear
            plan['steps'] = [{k: v for k, v in st.items() if k not in ('keep', 'armed')} for st in plan['steps']]
            if mode:
                self.fn_modes[name] = (mode, len(plan['steps']) > 1)
            self.fn_runs[name] = (changed, bool(rx))
            if rx:
                if replies:                              # one received word: wait until it is in SRAM
                    plan['steps'].append({'op': 'wait_bytes', 'count': 1})
                plan['steps'].append({'op': 'reply', **({'mask': mask} if mask is not None else {})})
            self.plans[name] = plan
        # A transaction on the result shifter receives a word even when the
        # call returns nothing (a send also samples): the call drains it into
        # SRAM before returning, so every call's result starts aligned.
        for fn, (runs, returns) in self.fn_runs.items():
            if not returns and runs & set(self.rx_via):
                self.plans[fn]['steps'].append({'op': 'wait_bytes', 'count': 1})
        self.mode_machine()

    def const_bytes(self, e, where):
        if e['kind'] == 'name' and e['name'] in self.lists:
            e = self.lists[e['name']]
        if e['kind'] != 'list':
            raise LowerError('compiler', f'{where}: bytes that are not a constant list are not lowered yet')
        out = [_int(v, self.env) for v in e['values']]
        if any(not 0 <= v < 256 for v in out):
            raise LowerError('compiler', f'{where}: a value outside a byte')
        return out

    def reply_shifter(self, name, replies):
        if len(replies) != 1:
            raise LowerError('compiler', f'{name}: a result needs one `reply` (or `-> bytes via <shifter>`)')
        v, mask = replies[0]['value'], None
        if v['kind'] == 'binary' and v['op'] == '&' and v['right']['kind'] == 'integer':
            v, mask = v['left'], _int(v['right'], self.env)
        x = v['object'].get('name') if v['kind'] == 'field' and v['field'] == 'received' else None
        if x not in self.res or self.res[x]['resource_kind'] != 'shifter':
            raise LowerError('compiler', f'{name}: a result other than a shifter\'s received word is not lowered yet')
        return x, mask

    def mode_machine(self):
        """Routes and pad drive that calls `set` before their transaction
        become a machine the host switches (a preconfigured context per
        mode): one state per distinct mode, entered by a host event at the
        start of a call. A call that sets nothing leaves the mode as it is."""
        self.mode_route = {}
        if not self.fn_modes:
            return
        keys = sorted({k for m, _ in self.fn_modes.values() for k in m})
        modes, of = [], {}
        for fn, (m, _) in self.fn_modes.items():
            if set(m) != set(keys):
                raise LowerError('compiler', f'{fn}: sets {sorted(m)} but other calls set {keys}; a call must set '
                                 'every mode-selected route and pad drive (a partial mode is not lowered yet)')
            sig = json.dumps([_strip(m[k]) for k in keys], sort_keys=True)
            if sig not in modes:
                modes.append(sig)
            of[fn] = modes.index(sig)
        values = [[json.loads(sig)[i] for sig in modes] for i in range(len(keys))]
        for k, v in zip(keys, values):
            if k[0] == 'sample' and len({json.dumps(x['left'], sort_keys=True) for x in v}) > 1:
                raise LowerError('compiler', f'{k[1]}: calls sample different data; only the sampling event may '
                                 'change by mode')
        if len(modes) == 1:
            for k, v in zip(keys, values):
                self.mode_route[k] = (v[0]['left'], v[0]['right']) if k[0] == 'sample' else \
                    {'kind': 'integer', 'value': str(v[0])}
            return
        n = len(modes)
        if n > self.limits['states']:
            raise LowerError('hardware', f'{n} distinct modes set by calls; a mode machine holds {self.limits["states"]}')
        if n - 1 > self.limits['transitions']:
            raise LowerError('hardware', f'{n} modes need {n - 1} transitions per state; this fabric takes '
                             f'{self.limits["transitions"]}')
        events = [f'_mode{i}' for i in range(n)]
        self.machines['_mode'] = {'name': '_mode', 'states': [
            {'name': f'M{j}', 'on': [{'event': {'kind': 'name', 'name': events[i]}, 'to': f'M{i}'}
                                     for i in range(n) if i != j]} for j in range(n)]}
        self.declared += [e for e in events if e not in self.declared]
        for fn, j in of.items():
            self.plans[fn]['steps'].insert(1, {'op': 'fire', 'event': events[j]})
            if events[j] not in self.host_events:
                self.host_events.append(events[j])

        def select(vals):
            e = vals[-1]
            for j in range(n - 2, -1, -1):
                e = {'kind': 'if', 'condition': {'kind': 'field', 'object': {'kind': 'name', 'name': '_mode'},
                                                  'field': f'M{j}'},
                     'yes': vals[j], 'no': e}
            return e
        for k, v in zip(keys, values):
            self.mode_route[k] = (v[0]['left'], select([x['right'] for x in v])) if k[0] == 'sample' else \
                select([{'kind': 'integer', 'value': str(x)} for x in v])

    # ---- resources
    def pin_drive(self, name):
        """An output pin's drive signal; pins overridden in contexts selected
        by one level (`contexts(sel) { context 0 {..} context 1 {..} }`) are a
        mux of the selector, computed by a LUT (one clock late, like every
        combinational LUT; a pin pair switched together keeps its timing)."""
        conns = [c for c in self.inputs if c['consumer'] == name and c['port'] == 'pin.drive']
        if not any(c['context'] is not None for c in conns):
            drive = self.conns(name).get('pin.drive')
            return None if drive is None else self.signal(drive)
        by = {}
        for c in conns:
            if c['check']['status'] != 'valid':
                raise LowerError('compiler', f'{name}.pin.drive: frontend check {c["check"]["status"]}')
            by[None if c['context'] is None else _int(c['context'], self.env)] = c['expression']
        if set(by) - {None, 0, 1}:
            raise LowerError('compiler', f'pin {name}: contexts {sorted(k for k in by if k is not None)} '
                             '(one selector level: contexts 0 and 1)')
        sel = [i for i in self.items if i['kind'] == 'context_selector']
        if len(sel) != 1:
            raise LowerError('compiler', f'pin {name}: {len(sel)} context selectors')
        if not self.logic:
            raise LowerError('hardware', f'pin {name}: a context-selected drive needs a logic cell')
        e0, e1 = by.get(0, by.get(None)), by.get(1, by.get(None))
        expr = {'kind': 'if', 'condition': sel[0]['signal'], 'yes': e1, 'no': e0}
        return self.comb_lut(expr, f'pin {name} (contexts)')

    def contexts_supported(self):
        """Contexts lower only as pin overrides (pin_drive)."""
        for i in self.items:
            if i['kind'] == 'context':
                other = [b for b in i['body'] if b.get('kind') != 'pin']
                if other:
                    raise LowerError('compiler', f'context {i["index"].get("value")}: overrides of '
                                     f'{sorted({b.get("kind") for b in other})} are not lowered yet (pins only)')

    def lut_timers(self, tiles, connect):
        """Timers past the contract's timer cells: the smallest (every and
        first at most 8 clocks, no aux input) become LUTs, an exact copy of
        grid_timer: a one-hot count R_N..R_1 (R_j: j clocks remain), a
        running register and the outputs read (tick, level, rise, fall).
        Start loads R_first if not running, R_1 ticks, toggles level and
        reloads R_period, stop clears everything (it wins)."""
        timers = [n for n, t in tiles.items() if t['kind'] == 'timer']
        excess = len(timers) - (self.timer_cells if self.timer_cells is not None else len(timers))
        if excess <= 0 or not self.logic:
            return
        small = sorted((max(tiles[n]['first'], tiles[n]['period']), n) for n in timers
                       if max(tiles[n]['first'], tiles[n]['period']) <= 8 and not tiles[n].get('mode')
                       and f'{n}.aux' not in connect)
        if len(small) < excess:
            raise LowerError('hardware', f'{len(timers)} timers, {self.timer_cells} timer cells, and only '
                             f'{len(small)} small enough (8 clocks) for logic cells')
        K = lambda k: {'kind': 'integer', 'value': str(k)} if isinstance(k, int) else {'kind': 'key', 'key': k}
        AND = lambda a, b: {'kind': 'binary', 'op': '&&', 'left': a, 'right': b}
        OR = lambda a, b: {'kind': 'binary', 'op': '||', 'left': a, 'right': b}
        NOT = lambda a: {'kind': 'unary', 'op': '!', 'value': a}
        XOR = lambda a, b: {'kind': 'binary', 'op': '^', 'left': a, 'right': b}
        for _, n in small[:excess]:
            tiles.pop(n)
            tag = f'~T{n}'
            for out in ('tick', 'level', 'rise', 'fall'):
                self.sig_alias[f'{n}.{out}'] = f'lut:{tag}_{out}'
        for _, n in small[:excess]:
            tag = f'~T{n}'
            al = lambda v: mux_key(v) if isinstance(v, dict) else self.sig_alias.get(v, v)
            st, sp = al(connect.pop(f'{n}.start', 0)), al(connect.pop(f'{n}.stop', 0))
            first, period = self._timer_spec(n)
            N = max(first, period)
            R = lambda j: K(f'lut:{tag}_r{j}')
            run = K(f'lut:{tag}_run')
            go = lambda e: e if sp == 0 else AND(NOT(K(sp)), e)
            where = f'timer {n} (in logic)'
            self.raw_lut(f'{tag}_run', go(OR(run, K(st))) if st != 0 else {'kind': 'integer', 'value': '0'}, where)
            for j in range(1, N + 1):
                terms = []
                if j < N:
                    terms.append(R(j + 1))
                if j == first and st != 0:
                    terms.append(AND(K(st), NOT(run)))
                if j == period:
                    terms.append(R(1))
                e = terms[0]
                for x in terms[1:]:
                    e = OR(e, x)
                self.raw_lut(f'{tag}_r{j}', go(e), where)
            outs = {'tick': R(1), 'level': XOR(K(f'lut:{tag}_level'), R(1)),
                    'rise': AND(R(1), NOT(K(f'lut:{tag}_level'))), 'fall': AND(R(1), K(f'lut:{tag}_level'))}
            used = {o for o in outs if self._reads(f'{n}.{o}', connect)}
            if used & {'rise', 'fall'}:
                used.add('level')
            for out, e in outs.items():
                if out in used:
                    self.raw_lut(f'{tag}_{out}', go(e), where)
        # LUTs read the converted timers' outputs through the alias too
        for l in self.luts.values():
            l['leaves'] = [self.sig_alias.get(k, k) for k in l['leaves']]
            if l['enable'] in self.sig_alias:
                l['enable'] = self.sig_alias[l['enable']]

    def _reads(self, sig, connect):
        """Is a signal read anywhere (a route, a machine, a host wait, a LUT)?"""
        text = json.dumps([list(connect.values()), [f['on'] for f in self.fsms.values()],
                           list(self.signals.values()),
                           [st.get('signal') for pl in self.plans.values() for st in pl['steps']],
                           [l['leaves'] + [l['enable']] for l in self.luts.values()]])
        return f'"{sig}"' in text

    def _timer_spec(self, n):
        return self._timer_first[n], self._timer_period[n]

    def resolve_luts(self, tiles, connect, kind):
        """Pack the LUTs into logic tiles and replace every LUT reference by
        its tile output; flag-set steps become parameter writes."""
        if self.sig_alias:
            def ren(v):
                if isinstance(v, str):
                    return self.sig_alias.get(v, v)
                if isinstance(v, dict):
                    return {k: ren(x) for k, x in v.items()}
                if isinstance(v, list):
                    return [ren(x) for x in v]
                return v
            for k in list(connect):
                connect[k] = ren(connect[k])
            for f in self.fsms.values():
                f['on'] = ren(f['on'])
            for n in list(self.signals):
                self.signals[n] = ren(self.signals[n])
            for pl in self.plans.values():
                pl['steps'] = [dict(st, signal=ren(st['signal'])) if 'signal' in st else st for st in pl['steps']]
        if not self.luts:
            for plan in self.plans.values():
                plan['steps'] = [st for st in plan['steps'] if st['op'] != 'flags']
            return
        # LUTs read by routes, machines or host waits (not only by LUTs)
        exported = set()

        def scan(v):
            if isinstance(v, str) and v.startswith('lut:'):
                exported.add(v[4:])
            elif isinstance(v, str) and v.startswith('@mux:'):
                scan(unkey(v))
            elif isinstance(v, dict):
                for x in v.values():
                    scan(x)
            elif isinstance(v, (list, tuple)):
                for x in v:
                    scan(x)
        scan(list(connect.values()))
        scan([f['on'] for f in self.fsms.values()])
        scan(list(self.signals.values()))
        scan([st.get('signal') for pl in self.plans.values() for st in pl['steps']])
        lt, lc, alias, slots = tiles_of(self.luts, kind, self.flag_init, exported)

        def res(v):
            if isinstance(v, str) and v.startswith('lut:'):
                return alias[v]
            if isinstance(v, str) and v.startswith('@mux:'):
                return res(unkey(v))
            if isinstance(v, dict):
                return {k: res(x) for k, x in v.items()}
            return v
        tiles.update(lt)
        for k, v in lc.items():
            connect[k] = v
        for k in list(connect):
            connect[k] = res(connect[k])
        for f in self.fsms.values():
            f['on'] = [[a, res(sig), b] for a, sig, b in f['on']]
        for n in list(self.signals):
            self.signals[n] = res(self.signals[n])
        for plan in self.plans.values():
            steps = []
            for st in plan['steps']:
                if st['op'] == 'flags':
                    steps += flag_writes(slots, self.flag_init, st['values'])
                elif st['op'] == 'wait':
                    steps.append(dict(st, signal=res(st['signal'])))
                else:
                    steps.append(st)
            plan['steps'] = steps

    def lower(self):
        unsupported = [i for i in self.items if i['kind'] in ('flags', 'context', 'context_selector')]
        if self.logic:
            self.contexts_supported()
            unsupported = []
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
        tiles.update(self.synth_tiles)
        for name, p in self.pins.items():
            if p['mode'] in ('out', 'open_drain'):
                drive = self.pin_drive(name)
                if drive is None:
                    raise LowerError('compiler', f'output pin {name} is not driven')
                d = f'{name}_out'
                drivers[d] = {'pin': name, 'push_pull': p['mode'] == 'out'}
                connect[f'{d}.value'] = drive
                en = self.mode_route.get(('drive', name))
                if name in self.drive_flag:
                    connect[f'{d}.enable'] = self.comb_lut({'kind': 'name', 'name': self.drive_flag[name]},
                                                           f'pin {name} enable')
                else:
                    connect[f'{d}.enable'] = 1 if en is None else self.expr_signal(en, f'pin {name} drive')
        words = self.streams(tiles)
        self.pin_reference(tiles, connect)
        self._timer_first = {n: t['first'] for n, t in tiles.items() if t['kind'] == 'timer'}
        self._timer_period = {n: t['period'] for n, t in tiles.items() if t['kind'] == 'timer'}
        self.lut_timers(tiles, connect)
        self.resolve_luts(tiles, connect, self.logic)
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
        if self.lags:
            design['lags'] = self.lags
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
            ports = {k: v for k, v in ports.items() if k != 'timer.run'}     # `run: 1` is the held start
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
        route = self.mode_route.get(('sample', name))
        if route is not None:                    # set by calls: the data, and the event by mode
            connect[f'{name}.in'] = self.expr_signal(route[0], f'{name} sample data')
            spec['io']['in'] = True
            at = self.expr_signal(route[1], f'{name} sample event')
            if at is None:
                raise LowerError('hardware', f'{name}: the sampling event chosen by mode needs logic')
            connect[f'{name}.sample'] = at
            spec['step']['sample'] = 'sample'
            ports = {k: v for k, v in ports.items() if k not in ('shifter.sample_at', 'shifter.sample_data')}
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
        if 'shifter.end' in ports:
            # A burst-end level that is constant 0 (a constant, or a flag no
            # call sets and that starts at 0) is the shifter's level 0.
            e = ports['shifter.end']['expression']
            const0 = (e['kind'] == 'integer' and _int(e, self.env) == 0) or \
                (e['kind'] == 'name' and e['name'] in self.flag_init and not self.flag_init[e['name']] and
                 not any(st['op'] == 'flags' and st['values'].get(e['name'])
                         for pl in self.plans.values() for st in pl['steps']))
            if not const0:
                raise LowerError('hardware', f'{name}: a burst-end level from a signal (the mesh shifter has no '
                                 'end sink)')
            ports = {k: v for k, v in ports.items() if k != 'shifter.end'}
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
        for key in ('restart', 'crc.restart'):
            if key in ports:
                connect[f'{name}.init'] = self.signal(ports[key])
                spec['init'] = 'init'
        for port, sink in (('crc.absorb', 'absorb'), ('crc.emit', 'emit'), ('crc.data', 'data')):
            if port in ports:
                connect[f'{name}.{sink}'] = self.signal(ports[port])
                spec[sink] = sink
        extra = set(ports) - {'restart', 'crc.restart', 'crc.link', 'crc.absorb', 'crc.emit', 'crc.data'}
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
        for sh in sorted(self.loaded):
            if sh in tx_sh:
                raise LowerError('hardware', f'{sh}: loaded by calls and fed by a stream; the word network feeds a '
                                 'shifter or its holding register does, not both')
            tiles[sh]['io']['auto_pull'] = True
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


def lower(ir, name, pads, contract, prefer_lut=False):
    lw = Lowering(ir, name, pads)
    lw.prefer_lut = prefer_lut
    lw.latency = contract['latency']
    lw.registers = contract['registers']
    lw.limits = {'states': contract['layout']['contexts'], 'transitions': contract['layout']['transition_rows']}
    if any(c['kind'] == 'logic' for c in contract['cells']):
        lw.logic = contract['kinds']['logic']
    lw.timer_cells = sum(1 for c in contract['cells'] if c['kind'] == 'timer')
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
