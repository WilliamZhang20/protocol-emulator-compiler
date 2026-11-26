#!/usr/bin/env python3
"""Compile protocol designs onto the reorganised fabric (RTL repository,
FABRIC_REORG.md, promotion criterion 3): place, route with contexts,
emit a configuration vector, and check it independently.

Source (one JSON per design; nothing in it names a cell, track or
context number):

  pins     {name: pad index}
  events   {name: {pin, edge: level|rise|fall, when?: {pin, level}}}   I/O tile outputs
  drivers  {name: {pin}}                       consumers name.value, name.enable (open drain)
  tiles    {name: {kind: timer, first, period, mode?, phase?}
                 | {kind: shifter, frame, step: {shift, sample}, io: {start?, in?, auto_push?,
                    auto_pull?}, abort?, burst?, coder5?, coder6?, word_port?}}
           shifter line fields name the tile's sinks (start, shift, sample, abort)
  words    {writer, gate, ... (word-network sinks <- sources), cmp_value, cmp_mask, tap}
  fsms     {name: {states: [reset, ...], on: [[from, signal, to], ...]}}  (order = priority)
  signals  {name: {fsm, cases: {state: ref}, default?: ref}}           muxed signals
  connect  {consumer: ref | {fsm, cases, default?}}

A ref is an event, a signal, `tile.output`, `words.match`/`words.mismatch`
or 0/1. Consumers are `tile.sink`, `driver.value`/`driver.enable` and
`words.gate`.

Hardware rules the compiler enforces (FABRIC_REORG.md, criteria 1-2):
- an FSM owns one contexted switch; its reset state is context 0; a state
  has at most two transitions (row A has priority);
- a muxed signal changes only at a selector in its FSM's switch: a sink of
  that cell (per context), or one track leaving that cell (X); every other
  selector is identical in every context of its switch, so a route
  through another design's contexted switch is never disturbed;
- a track cannot carry constant 1, so a mux with a constant-1 case is
  consumed only at its FSM's own cell.
"""
import heapq
import itertools
import json
import random
from pathlib import Path

from fabric_arch import DIRS, RRG, ConfigSim, config_bits


class MapError(Exception):
    """A mapping failure with its class: 'resources' (proven by counting),
    'rules' (the source breaks a hardware rule), 'routing' (heuristic)."""

    def __init__(self, cls, msg):
        super().__init__(f'{cls}: {msg}')
        self.cls = cls


# ---------------------------------------------------------------- source

EDGE = {'level': 0, 'rise': 1, 'fall': 2}


class Netlist:
    def __init__(self, designs):
        self.blocks = {}       # name -> {kind, design, ...}
        self.signals = {}      # id -> base | const | mux
        self.consumers = []    # {block, sink, sig}
        self.pins = {}         # pad -> design
        self.words = None
        for d in designs:
            self._add(d)
        self._flatten()
        self._check()

    def _add(self, d):
        dn = d['name']
        q = lambda n: f'{dn}/{n}'
        pins = d.get('pins', {})
        for name, pad in pins.items():
            if pad in self.pins:
                raise MapError('rules', f'pad {pad} used by {self.pins[pad]} and {dn}')
            self.pins[pad] = dn
        for name, e in d.get('events', {}).items():
            b = q(name)
            when = e.get('when')
            self.blocks[b] = {'kind': 'io_out', 'design': dn, 'pin': pins[e['pin']], 'edge': EDGE[e['edge']],
                              'qual': (pins[when['pin']], when['level']) if when else None}
            self.signals[b] = {'type': 'base', 'block': b, 'local': None}
        for name, v in d.get('drivers', {}).items():
            self.blocks[q(name)] = {'kind': 'io_drv', 'design': dn, 'pin': pins[v['pin']]}
        for name, t in d.get('tiles', {}).items():
            b = q(name)
            self.blocks[b] = dict(t, design=dn)
            for k, local in enumerate(['tick', 'level', 'rise', 'fall'] if t['kind'] == 'timer' else ['word', 'stop']):
                self.signals[f'{b}.{local}'] = {'type': 'base', 'block': b, 'local': k}
        for name, f in d.get('fsms', {}).items():
            self.blocks[q(name)] = {'kind': 'fsm', 'design': dn, 'states': f['states'], 'on_src': f.get('on', [])}
        if 'words' in d:
            if self.words is not None:
                raise MapError('resources', 'two designs use the one word network')
            ports = [n for n, t in d['tiles'].items() if t.get('word_port')]
            if len(ports) != 1:
                raise MapError('rules', f'{dn}: words need exactly one word_port shifter')
            self.words = dict(d['words'], design=dn, shifter=q(ports[0]))
            self.signals['#match'] = {'type': 'base', 'block': '#wp', 'local': 2}
            self.signals['#mismatch'] = {'type': 'base', 'block': '#wp', 'local': 3}
        self.signals['#0'] = {'type': 'const', 'value': 0}
        self.signals['#1'] = {'type': 'const', 'value': 1}
        anon = itertools.count()

        def ref(r):
            if isinstance(r, int) or (isinstance(r, str) and r in ('0', '1')):
                return f'#{int(r)}'
            if isinstance(r, dict):
                n = f'{dn}/~mux{next(anon)}'
                mux(n, r)
                return n
            if r in ('words.match', 'words.mismatch'):
                return '#' + r.split('.')[1]
            if q(r) in self.signals or r in d.get('signals', {}):
                return q(r)
            raise MapError('rules', f'{dn}: unknown signal {r}')

        def mux(n, m):
            f = q(m['fsm'])
            if f not in self.blocks or self.blocks[f]['kind'] != 'fsm':
                raise MapError('rules', f'{dn}: {n} names unknown fsm {m["fsm"]}')
            states = self.blocks[f]['states']
            cases = {}
            for s, r in m['cases'].items():
                if s not in states:
                    raise MapError('rules', f'{dn}: {n}: {s} is not a state of {m["fsm"]}')
                cases[states.index(s)] = r
            self.signals[n] = {'type': 'mux', 'fsm': f, 'cases_src': cases, 'default_src': m.get('default', 0)}

        for name, m in d.get('signals', {}).items():
            mux(q(name), m)
        for n, s in list(self.signals.items()):
            if s['type'] == 'mux' and 'cases_src' in s:
                s['cases'] = {k: ref(r) for k, r in s.pop('cases_src').items()}
                s['default'] = ref(s.pop('default_src'))
        for name, f in d.get('fsms', {}).items():
            b = self.blocks[q(name)]
            st = b['states']
            on = []
            for frm, sig, to in b.pop('on_src'):
                on.append((st.index(frm), ref(sig), st.index(to)))
            b['on'] = on
        for cons, r in d.get('connect', {}).items():
            blk, sink = cons.rsplit('.', 1)
            if blk == 'words':
                target = '#wp'
            else:
                target = q(blk)
                if target not in self.blocks:
                    raise MapError('rules', f'{dn}: unknown consumer {cons}')
            self.consumers.append({'block': target, 'sink': sink, 'sig': ref(r)})
        # resolve anonymous muxes created by ref() after the first pass
        for n, s in list(self.signals.items()):
            if s['type'] == 'mux' and 'cases_src' in s:
                s['cases'] = {k: ref(r) for k, r in s.pop('cases_src').items()}
                s['default'] = ref(s.pop('default_src'))

    def case(self, sig, state):
        s = self.signals[sig]
        return s['cases'].get(state, s['default'])

    def _flatten(self):
        # A case of mux M that is another mux of the same FSM is that mux's case.
        for n, s in self.signals.items():
            if s['type'] != 'mux':
                continue
            for st in range(len(self.blocks[s['fsm']]['states'])):
                seen = set()
                v = self.case(n, st)
                while self.signals[v]['type'] == 'mux' and self.signals[v]['fsm'] == s['fsm']:
                    if v in seen:
                        raise MapError('rules', f'mux loop at {n}')
                    seen.add(v)
                    v = self.case(v, st)
                s['cases'][st] = v
            s['default'] = '#0' if all(st in s['cases'] for st in range(len(self.blocks[s['fsm']]['states']))) else s['default']

    def _check(self):
        for b, v in self.blocks.items():
            if v['kind'] == 'fsm':
                if len(v['states']) > 4:
                    raise MapError('resources', f'{b}: {len(v["states"])} states, at most 4 contexts')
                for st in range(len(v['states'])):
                    n = sum(1 for f, _, _ in v['on'] if f == st)
                    if n > 2:
                        raise MapError('resources', f'{b}: state {v["states"][st]} has {n} transitions, at most 2')
        # Mux dependency must be acyclic (no loop through switches).
        order = self.mux_order()
        self.mux_rank = {m: i for i, m in enumerate(order)}

    def mux_alts(self, m):
        s = self.signals[m]
        n = len(self.blocks[s['fsm']]['states'])
        return sorted({self.case(m, st) for st in range(n)})

    def mux_order(self):
        """Muxes with every consumer mux before them (consumer-first)."""
        muxes = [n for n, s in self.signals.items() if s['type'] == 'mux']
        feeds = {m: [a for a in self.mux_alts(m) if self.signals[a]['type'] == 'mux'] for m in muxes}
        out, state = [], {}

        def visit(m):
            if state.get(m) == 1:
                raise MapError('rules', f'combinational loop through mux {m}')
            if state.get(m) == 2:
                return
            state[m] = 1
            for a in feeds[m]:
                visit(a)
            state[m] = 2
            out.append(m)
        for m in muxes:
            visit(m)
        return list(reversed(out))   # consumers first


# ------------------------------------------------------------- placement

class Placement:
    def __init__(self, arch, nl):
        self.arch, self.nl = arch, nl
        a = arch
        self.cells_of = {k: [c for c in range(a.n) if a.kind(c) == k] for k in ('io', 'timer', 'shifter')}
        self.ctx_cells = [c for c in range(a.n) if a.contexts(c) == 4]
        self.wp = a.c['word_port']['cell']
        need = {'io_out': 0, 'io_drv': 0, 'timer': 0, 'shifter': 0, 'fsm': 0}
        for v in nl.blocks.values():
            need[v['kind']] += 1
        have = {'io_out': 4 * len(self.cells_of['io']), 'io_drv': len(self.cells_of['io']),
                'timer': len(self.cells_of['timer']), 'shifter': len(self.cells_of['shifter']),
                'fsm': len(self.ctx_cells)}
        short = [f'{k} {need[k]} > {have[k]}' for k in need if need[k] > have[k]]
        if short:
            raise MapError('resources', 'not enough ' + ', '.join(short))
        self.need, self.have = need, have
        # FSMs pinned by constant-1 muxes consumed at a fixed cell or block.
        self.fixed = {}
        self.colocate = []     # (fsm, block): fsm must sit in the block's cell
        for c in nl.consumers:
            s = nl.signals[c['sig']]
            if s['type'] == 'mux' and '#1' in nl.mux_alts(c['sig']):
                if c['block'] == '#wp':
                    if self.fixed.setdefault(s['fsm'], self.wp) != self.wp:
                        raise MapError('rules', f'{s["fsm"]} pinned to two cells')
                else:
                    self.colocate.append((s['fsm'], c['block']))
        self.links = self._links()

    def _links(self):
        """(block, block) pairs whose cells should be close: every signal
        hop from its producer to where it is read."""
        nl, links = self.nl, []

        def src(sig):
            s = nl.signals[sig]
            return s['block'] if s['type'] == 'base' else s['fsm'] if s['type'] == 'mux' else None

        def read(sig, at):
            s = nl.signals[sig]
            if s['type'] == 'mux':
                for a in nl.mux_alts(sig):
                    if src(a):
                        links.append((src(a), s['fsm']))
            if src(sig) and at:
                links.append((src(sig), at))
        for c in nl.consumers:
            read(c['sig'], c['block'])
        for b, v in nl.blocks.items():
            if v['kind'] == 'fsm':
                for _, sig, _ in v['on']:
                    read(sig, b)
        return links

    def cell(self, binding, b):
        if b == '#wp':
            return self.wp
        return binding[b][0] if isinstance(binding[b], tuple) else binding[b]

    def cost(self, binding):
        c = sum(self.arch.manhattan(self.cell(binding, a), self.cell(binding, b)) for a, b in self.links)
        for f, b in self.colocate:
            if self.cell(binding, f) != self.cell(binding, b):
                c += 100
        return c

    def domain(self, b):
        v = self.nl.blocks[b]
        k = v['kind']
        if k == 'fsm':
            if b in self.fixed:
                return [self.fixed[b]]
            return self.ctx_cells
        if k in ('io_out', 'io_drv'):
            return self.cells_of['io']
        return self.cells_of[k]

    def capacity(self, b):
        return 4 if self.nl.blocks[b]['kind'] == 'io_out' else 1

    def legal(self, binding):
        use = {}
        for b, c in binding.items():
            key = (self.group(b), c)
            use[key] = use.get(key, 0) + 1
            if use[key] > self.capacity(b):
                return False
        return True

    def group(self, b):
        k = self.nl.blocks[b]['kind']
        return 'tile' if k in ('timer', 'shifter') else k

    def random_binding(self, rng):
        for _ in range(1000):
            binding, use = {}, {}
            ok = True
            for b in sorted(self.nl.blocks, key=lambda b: len(self.domain(b))):
                opts = [c for c in self.domain(b) if use.get((self.group(b), c), 0) < self.capacity(b)]
                if not opts:
                    ok = False
                    break
                c = rng.choice(opts)
                binding[b] = c
                use[(self.group(b), c)] = use.get((self.group(b), c), 0) + 1
            if ok:
                return binding
        raise MapError('resources', 'no legal placement found')

    def anneal(self, seed, moves=4000):
        rng = random.Random(seed)
        binding = self.random_binding(rng)
        cost = self.cost(binding)
        blocks = list(self.nl.blocks)
        t = 3.0
        for i in range(moves):
            b = rng.choice(blocks)
            dom = self.domain(b)
            if len(dom) < 2:
                continue
            new = dict(binding)
            c = rng.choice(dom)
            # swap with a block of the same group in the target cell if full
            others = [o for o in blocks if o != b and self.group(o) == self.group(b) and new[o] == c]
            if len(others) >= self.capacity(b):
                o = rng.choice(others)
                if new[b] not in self.domain(o):
                    continue
                new[o] = new[b]
            new[b] = c
            if not self.legal(new):
                continue
            nc = self.cost(new)
            if nc <= cost or rng.random() < pow(2.718, (cost - nc) / t):
                binding, cost = new, nc
            t = max(0.05, t * 0.999)
        return binding, cost

    def slots(self, binding):
        """io_out blocks -> local index within their io cell."""
        out, used = {}, {}
        for b in sorted(binding):
            if self.nl.blocks[b]['kind'] == 'io_out':
                c = binding[b]
                out[b] = used.get(c, 0)
                used[c] = out[b] + 1
        return out


# --------------------------------------------------------------- routing

class Problem:
    """The routing problem of a placed netlist: nets (source, sink nodes),
    mux points, in-cell mux consumers and event reads."""

    def __init__(self, arch, nl, pl, binding):
        self.arch, self.nl, self.pl, self.binding = arch, nl, pl, binding
        self.slot = pl.slots(binding)
        self.fsm_cell = {b: binding[b] for b, v in nl.blocks.items() if v['kind'] == 'fsm'}
        self.ipin_of = {}      # consumer index -> ('ipin', cell, k)
        reads = {}             # sig -> set of sink nodes (static ipins, arrive)
        self.mux_in = {}       # mux -> [consumer ipins at its FSM cell]
        self.mux_out = {}      # mux -> sinks reached through X
        self.needs_x = set()

        def add_read(sig, node):
            s = nl.signals[sig]
            if s['type'] == 'const':
                return
            reads.setdefault(sig, set()).add(node)

        for i, c in enumerate(nl.consumers):
            node = self.ipin(c)
            self.ipin_of[i] = node
            s = nl.signals[c['sig']]
            if s['type'] == 'mux' and self.fsm_cell[s['fsm']] == node[1]:
                self.mux_in.setdefault(c['sig'], []).append(node)
            else:
                add_read(c['sig'], node)
        for b, v in nl.blocks.items():
            if v['kind'] != 'fsm':
                continue
            f = self.fsm_cell[b]
            for st, sig, _ in v['on']:
                s = nl.signals[sig]
                if s['type'] == 'mux' and s['fsm'] == b:
                    self.mux_in.setdefault(sig, [])          # read per context at f
                    for a in nl.mux_alts(sig):
                        add_read(a, ('arrive', f))
                else:
                    add_read(sig, ('arrive', f))
        # Muxes, consumers first: decide X and what the alternatives must reach.
        self.feed_target = {}
        for m in nl.mux_order():
            f = self.fsm_cell[nl.signals[m]['fsm']]
            outside = reads.pop(m, set())
            if outside:
                self.needs_x.add(m)
                self.mux_out[m] = outside
                if '#1' in nl.mux_alts(m):
                    raise MapError('rules', f'{m} has a constant-1 case but is read outside its FSM cell')
            target = ('feed', m) if m in self.needs_x else ('arrive', f)
            self.feed_target[m] = target
            if m in self.needs_x or m in self.mux_in:
                for a in nl.mux_alts(m):
                    add_read(a, target)
        self.nets = []
        for m in nl.mux_order():
            if m in self.needs_x:
                self.nets.append({'name': m, 'source': ('xsrc', m), 'sinks': sorted(self.mux_out[m], key=str)})
        for sig, sinks in reads.items():
            if nl.signals[sig]['type'] != 'base':
                raise MapError('rules', f'{sig} read but not routable')
            self.nets.append({'name': sig, 'source': self.opin(sig), 'sinks': sorted(sinks, key=str)})

    def ipin(self, c):
        nl, a = self.nl, self.arch
        if c['block'] == '#wp':
            if c['sink'] != 'gate':
                raise MapError('rules', f'words.{c["sink"]} is not a word-port sink')
            return ('ipin', self.pl.wp, a.c['word_port']['sinks']['gate'])
        v = nl.blocks[c['block']]
        cell = self.binding[c['block']]
        if v['kind'] == 'io_drv':
            k = {'value': 0, 'enable': 1}[c['sink']]
        else:
            sinks = a.c['kinds'][v['kind']]['sinks']
            if c['sink'] not in sinks or (c['sink'] == 'gate'):
                raise MapError('rules', f'{c["block"]}: no sink {c["sink"]}')
            k = sinks.index(c['sink'])
        return ('ipin', cell, k)

    def opin(self, sig):
        s = self.nl.signals[sig]
        if s['block'] == '#wp':
            return ('opin', self.pl.wp, s['local'])
        v = self.nl.blocks[s['block']]
        if v['kind'] == 'io_out':
            return ('opin', self.binding[s['block']], self.slot[s['block']])
        return ('opin', self.binding[s['block']], s['local'])


def route(rrg, prob, max_iterations=60, pres_fac=0.5, pres_mult=1.4, hist_fac=1.0):
    """PathFinder (negotiated congestion) with mux points: a mux net's
    source is a virtual node whose first hop is any track leaving its FSM
    cell (the X it picks); the nets feeding that mux must reach a node
    that drives X. Muxes route before their feeders each iteration."""
    nl = prob.nl
    capacity = lambda node: node[0] in ('track', 'ipin')
    history, occupancy = {}, {}
    trees = {n['name']: [] for n in prob.nets}
    xs, bad = {}, {m: set() for m in prob.needs_x}
    fcell = {m: prob.fsm_cell[nl.signals[m]['fsm']] for m in prob.needs_x}
    feed_from = {}

    def succ(node):
        if node[0] == 'xsrc':
            f = fcell[node[1]]
            return [(('track', f, d, t), None) for d in DIRS if rrg.neighbour(f, d) is not None
                    for t in range(rrg.tracks) if ('track', f, d, t) not in bad[node[1]]]
        return rrg.out.get(node, []) + feed_from.get(node, [])

    def node_cell(node):
        if node[0] == 'feed':
            return fcell[node[1]]
        if node[0] == 'xsrc':
            return fcell[node[1]]
        return rrg.cell_of(node)

    def cost(node, pres):
        if not capacity(node):
            return 0.0
        over = max(0, occupancy.get(node, 0) + 1 - 1)
        return (1.0 + history.get(node, 0.0)) * (1.0 + pres * over)

    def set_x(m, x):
        old = xs.get(m)
        if old == x:
            return
        if old is not None:
            for a, _ in rrg.into[old]:
                feed_from[a] = [e for e in feed_from.get(a, []) if e[0] != ('feed', m)]
        xs[m] = x
        for a, _ in rrg.into[x]:
            feed_from.setdefault(a, []).append((('feed', m), None))

    def route_net(net, pres):
        in_tree, edges, missing = {net['source']}, [], []
        src_cell = node_cell(net['source'])
        for sink in sorted(net['sinks'], key=lambda s: rrg.manhattan(src_cell, node_cell(s))):
            target = node_cell(sink)
            frontier = [(rrg.manhattan(node_cell(n), target), 0.0, i, n) for i, n in enumerate(in_tree)]
            heapq.heapify(frontier)
            best = {n: 0.0 for n in in_tree}
            parent, found, tie = {}, None, len(frontier)
            while frontier:
                _, g, _, node = heapq.heappop(frontier)
                if g > best.get(node, float('inf')):
                    continue
                if node == sink:
                    found = node
                    break
                for nxt, _ in succ(node):
                    if nxt in in_tree:
                        continue
                    if nxt[0] in ('ipin', 'arrive', 'feed') and nxt != sink:
                        continue
                    ng = g + cost(nxt, pres) + 0.01
                    if ng < best.get(nxt, float('inf')):
                        best[nxt] = ng
                        parent[nxt] = node
                        tie += 1
                        heapq.heappush(frontier, (ng + rrg.manhattan(node_cell(nxt), target), ng, tie, nxt))
            if found is None:
                missing.append(sink)
                continue
            n = found
            while n not in in_tree:
                p = parent[n]
                edges.append((p, n))
                in_tree.add(n)
                n = p
        return edges, missing

    pres = pres_fac
    for it in range(1, max_iterations + 1):
        retry = False
        for net in prob.nets:
            for _, b in trees[net['name']]:
                if capacity(b):
                    occupancy[b] -= 1
            edges, missing = route_net(net, pres)
            for s in missing:
                if s[0] == 'feed':
                    m = s[1]
                    bad[m].add(xs[m])
                    retry = True
                    if len(bad[m]) >= 4 * rrg.tracks:
                        raise MapError('routing', f'no track out of {m}\'s cell can be fed by all its alternatives')
                else:
                    raise MapError('routing', f'{net["name"]}: no path to {s}')
            trees[net['name']] = edges
            for _, b in edges:
                if capacity(b):
                    occupancy[b] = occupancy.get(b, 0) + 1
            if net['source'][0] == 'xsrc':
                x = next(b for a, b in edges if a == net['source'])
                set_x(net['name'], x)
        overused = [n for n, o in occupancy.items() if o > 1]
        if not overused and not retry:
            return {'iterations': it, 'trees': trees, 'x': dict(xs)}
        for n in overused:
            history[n] = history.get(n, 0.0) + hist_fac * (occupancy[n] - 1)
        pres *= pres_mult
    raise MapError('routing', f'congestion after {max_iterations} iterations ({len(overused)} overused nodes)')


# ------------------------------------------------------------------ emit

def emit(arch, prob, routed):
    """Configuration fields {(cell, 'track'|'sink'|'trans', ctx, ...): value}
    and the flat vector."""
    nl, rrg = prob.nl, arch
    fields = {}

    def put(key, v):
        if fields.setdefault(key, v) != v:
            raise MapError('routing', f'field {key} needs {fields[key]} and {v}')

    def sel_key(node, ctx):
        if node[0] == 'track':
            return (node[1], 'track', ctx, node[2], node[3])
        return (node[1], 'sink', ctx, node[2])

    trees = routed['trees']
    for name, edges in trees.items():
        for a, b in edges:
            if b[0] in ('track', 'ipin') and a[0] != 'xsrc':
                v = rrg.value(a, b)
                for ctx in range(4):
                    put(sel_key(b, ctx), v)

    def arrival(sig, cell, via=None):
        """The node at which `sig` is present at `cell`'s switch."""
        s = nl.signals[sig]
        if s['type'] == 'const':
            return None
        net = trees[sig]
        for a, b in net:
            if (via is not None and b == via) or (via is None and b == ('arrive', cell)):
                return a
        raise MapError('routing', f'{sig} does not arrive at cell {cell}')

    def const_value(sig, track):
        v = nl.signals[sig]['value']
        if track and v:
            raise MapError('rules', 'constant 1 on a track')
        return v

    for m, s in ((m, nl.signals[m]) for m in nl.mux_order()):
        f = prob.fsm_cell[s['fsm']]
        nst = len(nl.blocks[s['fsm']]['states'])
        target = prob.feed_target[m]
        if m in prob.needs_x:
            x = routed['x'][m]
            for ctx in range(4):
                alt = nl.case(m, ctx) if ctx < nst else '#0'
                if nl.signals[alt]['type'] == 'const':
                    put(sel_key(x, ctx), const_value(alt, True))
                else:
                    put(sel_key(x, ctx), rrg.value(arrival(alt, f, target), x))
        for ipin in prob.mux_in.get(m, []):
            for ctx in range(4):
                alt = nl.case(m, ctx) if ctx < nst else '#0'
                if nl.signals[alt]['type'] == 'const':
                    put(sel_key(ipin, ctx), const_value(alt, False))
                else:
                    put(sel_key(ipin, ctx), rrg.value(arrival(alt, f, target), ipin))
    sv = arch.c['encoding']['sink']
    for b, v in nl.blocks.items():
        if v['kind'] != 'fsm':
            continue
        f = prob.fsm_cell[b]
        for ctx in range(4):
            rows = [(sig, to) for st, sig, to in v['on'] if st == ctx]
            vals = []
            for sig, to in rows:
                s = nl.signals[sig]
                if s['type'] == 'mux' and s['fsm'] == b:
                    sig = nl.case(sig, ctx)
                    s = nl.signals[sig]
                if s['type'] == 'const':
                    ev = sv['one'] if s['value'] else sv['zero']
                else:
                    target = ('arrive', f)
                    if s['type'] == 'mux':
                        target = ('arrive', f)
                    node = arrival(sig, f)
                    ev = sel_value(arch, node, f)
                vals.append((ev, to))
            while len(vals) < 2:
                vals.append((0, 0))
            put((f, 'trans', ctx), tuple(vals))
    return fields


def sel_value(arch, node, cell):
    """Sink-selector value at `cell` reading `node` (a local output of the
    cell or a track arriving there)."""
    sv = arch.c['encoding']['sink']
    if node[0] == 'opin':
        return sv['local_first'] + node[2]
    for port in DIRS:
        if arch.neighbour(cell, port) == node[1] and node[2] == {'north': 'south', 'south': 'north', 'west': 'east', 'east': 'west'}[port]:
            return sv[f'{port}_first'] + node[3]
    raise ValueError(node)


def tile_config(arch, nl, pl, binding):
    """Per-cell tile configuration words {cell: int} and the word network."""
    tiles = {}
    slot = pl.slots(binding)
    lines = arch.c['kinds']['shifter']['lines']
    for b, v in nl.blocks.items():
        k = v['kind']
        if k == 'io_out':
            c = binding[b]
            qpin, qlev = v['qual'] or (0, 0)
            o = (v['pin'] & 7) | (v['edge'] << 3) | ((qpin & 7) << 5) | ((1 if v['qual'] else 0) << 8) | \
                ((0 if qlev else 1) << 9 if v['qual'] else 0)
            tiles[c] = tiles.get(c, 0) | (o << (10 * slot[b]))
        elif k == 'io_drv':
            c = binding[b]
            tiles[c] = tiles.get(c, 0) | (1 << 43) | ((v['pin'] & 7) << 40)
        elif k == 'timer':
            tiles[binding[b]] = (v['first'] | (v['period'] << 16) | (v.get('mode', 0) << 32) |
                                 (v.get('phase', 0) << 48))
        elif k == 'shifter':
            ln = lambda name: lines[name] if name else 0
            step = v.get('step', {})
            io = v.get('io', {})
            regs = [int(str(v.get('frame', 0)), 0),
                    ln(step.get('shift')) | (ln(step.get('sample')) << 6),
                    ln(io.get('start')) | ((arch.c['kinds']['shifter']['in_level'] - 32 if io.get('in') else 0) << 6) |
                    (int(bool(io.get('auto_pull'))) << 11) | (int(bool(io.get('auto_push'))) << 12),
                    int(str(v.get('burst', 0)), 0) | (ln(v.get('abort')) << 7),
                    int(str(v.get('coder5', 0)), 0), int(str(v.get('coder6', 0)), 0)]
            tiles[binding[b]] = sum(r << (16 * i) for i, r in enumerate(regs))
    w = nl.words
    src = {'reader': 1, 'rx': 2, 'status': 3, 'capture': 4, 'escape': 5, 'compare': 6, 'gate': 7, 'split': 8,
           'interleave': 9}
    sink = {'writer': 0, 'tx': 1, 'escape': 2, 'compare': 3, 'gate': 4, 'split': 5, 'interleave.a': 6,
            'interleave.b': 7}
    if w:
        sel = 0
        for k, s in w.items():
            if k in sink:
                sel |= src[s] << (4 * sink[k])
        wn = sel | (int(str(w.get('cmp_value', 0)), 0) << 72) | (int(str(w.get('cmp_mask', 0)), 0) << 88) | \
            (src.get(w.get('tap'), 0) << 105) | (binding[w['shifter']] << 109)
    else:
        wn = 15 << 109
    return tiles, wn


def pack(arch, fields, tiles, wnet):
    lay = arch.c['layout']
    bits = 0
    for key, v in fields.items():
        cell, what, ctx = key[:3]
        base = cell * lay['cell_bits']
        if what == 'track':
            d, t = key[3:]
            bits |= v << (base + lay['track_lsb'] + 48 * ctx + 12 * DIRS.index(d) + 3 * t)
        elif what == 'sink':
            bits |= v << (base + lay['sink_lsb'] + 5 * lay['sink_slots'] * ctx + 5 * key[3])
        else:
            (ea, na), (eb, nb) = v
            bits |= (ea | (na << 5) | (eb << 7) | (nb << 12)) << (base + lay['trans_lsb'] + 14 * ctx)
    for cell, v in tiles.items():
        bits |= v << (cell * lay['cell_bits'] + lay['tile_lsb'])
    bits |= wnet << (arch.n * lay['cell_bits'])
    return bits


# ----------------------------------------------------------------- check

def check(contract, nl, prob, bits):
    """Independent of routing and emission: decode the vector (ConfigSim)
    and, in every combination of FSM states, compare what each sink and
    transition row reads with the source's meaning. Unused sinks must read
    constant 0. Returns a list of problems."""
    sim = ConfigSim(contract, bits)
    fsms = [b for b, v in nl.blocks.items() if v['kind'] == 'fsm']
    fcell = {b: prob.fsm_cell[b] for b in fsms}
    problems = []
    used = {node for node in prob.ipin_of.values()}

    def expect(sig, states):
        s = nl.signals[sig]
        while s['type'] == 'mux':
            sig = nl.case(sig, states[s['fsm']])
            s = nl.signals[sig]
        return ('const', s['value']) if s['type'] == 'const' else prob.opin(sig)

    ranges = [range(len(nl.blocks[b]['states'])) for b in fsms]
    for combo in itertools.product(*ranges):
        states = dict(zip(fsms, combo))
        ctx = [0] * sim.n
        for b, st in states.items():
            ctx[fcell[b]] = st
        for i, c in enumerate(nl.consumers):
            node = prob.ipin_of[i]
            got = sim.sink(node[1], node[2], ctx)
            want = expect(c['sig'], states)
            if got != want:
                problems.append(f'{c["block"]}.{c["sink"]} in {states}: reads {got}, wants {want}')
        for cell in range(sim.n):
            for k in range(sim.nsinks(cell)):
                if ('ipin', cell, k) not in used and sim.sink(cell, k, ctx) != ('const', 0):
                    problems.append(f'unused sink {cell}.{k} in {states}: reads {sim.sink(cell, k, ctx)}')
        for b in fsms:
            f, st = fcell[b], states[b]
            ea, na, eb, nb = sim.trans(f, st)
            rows = [(sig, to) for s0, sig, to in nl.blocks[b]['on'] if s0 == st]
            got = [(sim.select(f, ea, ctx), na), (sim.select(f, eb, ctx), nb)]
            for i in range(2):
                want = (expect(rows[i][0], states), rows[i][1]) if i < len(rows) else (('const', 0), None)
                g = got[i] if i < len(rows) else (got[i][0], None)
                if g != want:
                    problems.append(f'{b} state {st} row {i}: {g}, wants {want}')
        if len(problems) > 20:
            break
    for cell in range(sim.n):
        if sim.contexts(cell) == 1:
            continue
        if cell not in fcell.values():
            for ctx in range(4):
                if sim.trans(cell, ctx) != (0, 0, 0, 0):
                    problems.append(f'cell {cell} has transitions but no FSM')
    return problems


# ------------------------------------------------------------------- top

def compile_designs(contract, designs, seeds=20, moves=4000):
    """Place, route, emit and check. Returns a result dict; raises MapError
    with its failure class."""
    arch = RRG(contract)
    nl = Netlist(designs)
    pl = Placement(arch, nl)
    last = None
    tried = []
    for seed in range(1, seeds + 1):
        binding, wl = pl.anneal(seed, moves)
        key = tuple(sorted(binding.items()))
        if key in tried:
            continue
        tried.append(key)
        try:
            prob = Problem(arch, nl, pl, binding)
            routed = route(arch, prob)
            fields = emit(arch, prob, routed)
        except MapError as e:
            last = e
            continue
        tiles, wn = tile_config(arch, nl, pl, binding)
        bits = pack(arch, fields, tiles, wn)
        problems = check(contract, nl, prob, bits)
        if problems:
            raise MapError('compiler', 'check failed: ' + '; '.join(problems[:5]))
        tracks = {b for edges in routed['trees'].values() for _, b in edges if b[0] == 'track'}
        return {'binding': binding, 'wirelength': wl, 'seed': seed, 'iterations': routed['iterations'],
                'tracks_used': len(tracks), 'bits': bits, 'prob': prob, 'routed': routed, 'netlist': nl,
                'placement': pl, 'arch': arch}
    raise last or MapError('routing', 'no placement routed')


def hexfile(contract, bits):
    """32-bit words, least significant first, for $readmemh."""
    n = (config_bits(contract) + 31) // 32
    return ''.join(f'{(bits >> (32 * i)) & 0xffffffff:08x}\n' for i in range(n))


if __name__ == '__main__':
    import argparse
    p = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    p.add_argument('contract', type=Path)
    p.add_argument('designs', nargs='+', type=Path)
    p.add_argument('--hex', type=Path)
    a = p.parse_args()
    c = json.loads(a.contract.read_text())
    r = compile_designs(c, [json.loads(d.read_text()) for d in a.designs])
    print(f'placed (seed {r["seed"]}, wirelength {r["wirelength"]}), routed in {r["iterations"]} iterations, '
          f'{r["tracks_used"]} track segments; check passed')
    for b, cell in sorted(r['binding'].items()):
        print(f'  {b:28s} cell {cell}')
    for m, x in r['routed']['x'].items():
        print(f'  mux {m} switches at {x}')
    if a.hex:
        a.hex.write_text(hexfile(c, r['bits']))
