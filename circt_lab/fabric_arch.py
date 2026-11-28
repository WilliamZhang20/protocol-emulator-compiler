#!/usr/bin/env python3
"""Architecture model of the reorganised fabric (RTL repository,
experiments/grid_fabric/FABRIC_REORG.md, Active plan): coarse tiles beside
contexted switches (reorg/ctx_switch.v) on a mesh (reorg/mesh.v).

A *mesh contract* (JSON) is the only description of the hardware the
compiler reads: grid size, per-cell tile kind and context count, the kinds'
local outputs and sinks, the word-port cell, the selector encodings and the
configuration layout. `make_contract` writes one for any grid and resource
mix; `verilog_params` gives the same values as mesh.v parameters, so the
RTL is built from the contract, never described twice.

`RRG` is the routing-resource graph (nodes: local outputs, tracks, sinks,
and the virtual 'arrive at a cell' node). `ConfigSim` decodes a
configuration vector with its own code (it shares nothing with the
emitter) for the independent check.
"""
import json
from pathlib import Path

DIRS = ['north', 'south', 'west', 'east']           # ctx_switch track_cfg order, low bits first
DELTA = {'north': (-1, 0), 'south': (1, 0), 'west': (0, -1), 'east': (0, 1)}
OPPOSITE = {'north': 'south', 'south': 'north', 'west': 'east', 'east': 'west'}
VERTICAL = {'north', 'south'}

KINDS = {
    # io: two pads per cell (A: sinks 0/1, B: sinks 2/3), each open drain or
    # push-pull with a polarity bit.
    'io': {'code': 0, 'locals': ['o0', 'o1', 'o2', 'o3'],
           'sinks': ['a_value', 'a_enable', 'b_value', 'b_enable'], 'pads': 2},
    'timer': {'code': 1, 'locals': ['tick', 'level', 'rise', 'fall'], 'sinks': ['start', 'stop', 'aux']},
    'shifter': {'code': 2, 'locals': ['word', 'stop', 'out', 'active'],
                'sinks': ['start', 'shift', 'sample', 'abort', 'in', 'end'],
                # fabric_shifter lines driven by the sinks; `in` is level 33,
                # `end` (the burst-end level) level 34.
                'lines': {'start': 1, 'shift': 2, 'abort': 3, 'sample': 4}, 'in_level': 33, 'end_level': 34,
                # combinational paths sink -> local, as the mesh cell presents
                # them: none. fabric_shifter's word_pulse and stop_pulse are
                # combinational in its lines, but the mesh registers them at
                # the cell boundary (mesh.v REG=1, FABRIC_REORG.md step 5), one
                # clock later, so every local output is a register and no
                # configuration closes a loop. A kind that lists paths here
                # is checked by the mapper (_no_comb_loop).
                'comb': {}},
    # crc: fabric_crc; its direct link reaches the shifter west (link 0)
    # and east (link 1) of it. The word-port cell's locals 1-3 and sink 5
    # are the word network's bit port.
    # Local 1 is the crc's serial output `out` (a register: the inverted top
    # bit); at the word port only with shape bit 10 (it is `avail` there
    # otherwise).
    'crc': {'code': 3, 'locals': ['ok', 'out', 'match', 'mismatch'],
            'sinks': ['init', 'absorb', 'emit', 'data', 'level', 'gate'],
            'lines': {'init': 1, 'absorb': 2, 'emit': 3}, 'levels': {'data': 33, 'level': 34},
            'links': ['west', 'east']},
    # counter: fabric_counter (an 8-bit counter with a limit; `at_least` is
    # count >= limit, `reached` pulses on the step that first reaches it).
    # Its limit can be rewritten by the host while the counter is idle
    # (mesh.v param port, register 0). Not one of KINDS' 2-bit codes: mesh.v
    # marks counter cells with the CNT parameter.
    'counter': {'code': 4, 'locals': ['at_least', 'reached', 'unused2', 'unused3'],
                'sinks': ['step', 'data', 'clear'], 'lines': {'step': 1, 'clear': 2}, 'levels': {'data': 33},
                # `reached` is combinational in fabric_counter, registered at
                # the cell boundary like the shifter's word and stop.
                'comb': {}},
    # logic: mesh_logic, eight 4-input LUTs with output registers (the
    # source's `reg`s) and four host flags. LUTs are packed into logic tiles
    # before placement (fabric_logic.py); a tile's external inputs are its
    # six sinks, its own registers and flags are read inside the tile, and
    # only q0-q3 leave it (the cell's four locals). Marked with the LOGIC
    # parameter (its KINDS bits are ignored); one context: its LUT field
    # (344 bits) is the tile field, the tracks of contexts 1-3 and the
    # transition rows ('field').
    'logic': {'code': 5, 'locals': ['q0', 'q1', 'q2', 'q3'], 'sinks': ['s0', 's1', 's2', 's3', 's4', 's5'],
              'luts': 8, 'outputs': 4, 'flags': 4, 'lut_bits': 40, 'select_bits': 5,
              'field': [['tile', 0, 144], ['track', 48, 144], ['trans', 0, 56]],
              # LUT input select: sink k, LUT register q k, flag h k, constants
              'select': {'sink_first': 0, 'q_first': 6, 'h_first': 14, 'zero': 18, 'one': 19},
              # enable select: 0 every clock, 1 + k sink k, 7 never
              'enable': {'always': 0, 'sink_first': 1, 'never': 7},
              'comb': {}},
}

ENCODING = {
    # ctx_switch track selector (3 bits)
    'track': {'zero': 0, 'local_first': 1, 'straight': 5, 'turn_a': 6, 'turn_b': 7, 'bits': 3},
    # turn inputs: for a vertical output turn_a is the west input, turn_b the
    # east; for a horizontal output turn_a north, turn_b south. Tracks below
    # `turn_split` turn onto vertical outputs only, the rest onto horizontal.
    'turn_inputs': {'vertical': ['west', 'east'], 'horizontal': ['north', 'south']},
    'turn_split': 2,
    # sink / event selector (5 bits)
    'sink': {'zero': 0, 'one': 1, 'local_first': 2, 'north_first': 6, 'south_first': 10,
             'west_first': 14, 'east_first': 18, 'bits': 5},
}

# Tile register field maps ([hi, lo]); the same as the hardware's
# (architecture/grid_5x3.json lane_core.registers, checked by
# test_fabric_lower.py): fabric_shifter registers 1..6, fabric_crc 0..7.
REGISTERS = {
    'shifter': {
        'frame': {'reg': 1, 'fields': {'length_minus_1': [3, 0], 'pre': [5, 4], 'pre_value': [6, 6], 'post': [8, 7],
                                       'post_value': [9, 9], 'parity': [11, 10], 'msb_first': [12, 12],
                                       'lead': [13, 13], 'end_on_sample': [14, 14], 'idle': [15, 15]},
                  'parity': {'none': 0, 'even': 1, 'odd': 2}},
        'step': {'reg': 2, 'fields': {'shift_line': [5, 0], 'sample_line': [11, 6]}},
        'io': {'reg': 3, 'fields': {'start_line': [5, 0], 'in_level': [10, 6], 'auto_pull': [11, 11],
                                    'auto_push': [12, 12]}},
        'burst': {'reg': 4, 'fields': {'end_level': [4, 0], 'burst': [5, 5], 'eop_ends_burst': [6, 6],
                                       'abort_line': [12, 7], 'end_follows': [13, 13]}},
        'coder': {'reg': 5, 'fields': {'stuff_run': [2, 0], 'runs_of_ones': [3, 3], 'nrzi': [4, 4], 'receive': [5, 5],
                                       'arm_at_first_zero': [6, 6], 'tail_invert': [7, 7], 'bit_strobe': [8, 8],
                                       'tail_length': [14, 9], 'manchester': [15, 15]}},
        'coder2': {'reg': 6, 'fields': {'stuff_gate_level': [4, 0], 'stuff_gated': [5, 5], 'crc_skip': [12, 6],
                                        'link_data_only': [13, 13]}},
        # Not a configuration register: mesh.v's holding register, a loaded
        # value the shifter sends when its transaction starts (param port only).
        'load': {'reg': 7, 'fields': {'value': [7, 0]}},
    },
    'counter': {
        'limit': {'reg': 0, 'fields': {'limit': [7, 0]}},
        'step': {'reg': 1, 'fields': {'step_line': [5, 0], 'data_level': [10, 6], 'mode': [12, 11]},
                 'mode': {'count': 0, 'ones': 1, 'zeros': 2, 'equal': 3}},
        'clear': {'reg': 2, 'fields': {'clear_line': [4, 0]}},
    },
    # A logic tile's host flags (param port while running: the flags in
    # mask data[7:4] take data[3:0]).
    'logic': {
        'flags': {'reg': 0, 'fields': {'h': [3, 0], 'mask': [7, 4]}},
    },
    'crc': {
        'shape': {'reg': 6, 'fields': {'width_minus_1': [4, 0], 'init_ones': [5, 5], 'linked': [6, 6],
                                       'link_select': [7, 7], 'additive': [8, 8], 'twos_complement': [9, 9],
                                       # mesh.v: at the word port, local 1 is `out` (not `avail`)
                                       'out_local': [10, 10]}},
    },
}

# Pin-to-tile latencies (clocks), from the RTL (mesh_tb measures them):
# a pin edge that starts a timer whose tick samples a pin samples it
# edge_to_sample clocks after `first`; a transmit frame's first bit lasts
# first + start_to_shift at the pad when the shifter's active starts the
# timer that shifts it.
LATENCY = {'edge_to_sample': 1, 'start_to_shift': 2}

# Route timing (FABRIC_REORG.md step 5). Routes are combinational across
# switches. One hop measured 137 ps before layout (IHP liberty, a chain of
# ctx_switch track paths); the tile and context-select logic around a route
# is at most 8.9 ns over every configuration (the mesh synthesized with its
# configuration free). The bound allows 0.5 ns per hop after layout within
# the 20 ns clock (8.9 + 12 x 0.5 < 20); the mapper refuses a placement
# whose longest route is longer. Step 6's post-layout STA recalibrates it.
TIMING = {'clock_ps': 20000, 'hop_ps_synth': 137, 'max_route_hops': 12}

LAYOUT = {'cell_bits': 512, 'track_lsb': 0, 'sink_lsb': 192, 'sink_slots': 6, 'trans_lsb': 312,
          'tile_lsb': 368, 'tile_bits': 144, 'wnet_bits': 128, 'contexts': 4, 'tracks': 4,
          'transition_rows': 2}

# Static/dynamic split (FABRIC_REORG.md O3, single-protocol study): with
# `dyn_slots` D > 0 a contexted cell stores each selector once (context 0's
# track and sink fields) and D dynamic slots in the bits of contexts 1-3's
# tracks, slot s at track_lsb + 48 + 20 s: index [4:0] (0 none, 1-16 track
# 4 x dir + t + 1, 17-22 sink k + 17), the selector's value in context k
# at [5 k +: 5] (k = 1..3). Contexts 1-3 read the static value except where
# a slot names the selector. Transition rows are as before.
SLOT = {'bits': 20, 'lsb': 48, 'track_first': 1, 'sink_first': 17}
# The fixed split (dyn_slots 0): `ctx_tracks`, a mask of the track indices
# whose selectors are held per context (0b1111 every track, the baseline);
# the others read context 0's selector in every context. Sinks per context.


# Control actor (ACTOR_FABRIC.md A2; mesh.v ACTOR=1): a logic cell's second
# mode, a state machine of `states` states over `inputs` sinks, `rows`
# transition rows per state, its four locals selected per state from the
# inputs and constants, combinationally. Sinks 6-7 sit where context 1's
# sinks 0-1 would be. Field (in the 344-bit logic field L): rows at
# [12s + 6r +: 6] = {next[5:4], event[3:0]} (0 none, 1-8 input 0-7);
# outputs at [48 + 16s + 4k +: 4] (0 zero, 1 one, 2-9 input 0-7); mode L[343].
ACTOR = {'states': 4, 'rows': 2, 'inputs': 8, 'outputs': 4, 'row_lsb': 0, 'row_bits': 6,
         'out_lsb': 48, 'out_bits': 4, 'mode_bit': 343,
         'event': {'none': 0, 'input_first': 1}, 'select': {'zero': 0, 'one': 1, 'input_first': 2}}


def make_contract(rows, cols, kinds, contexts, word_port, name=None, dyn_slots=0, ctx_tracks=0b1111, actor=False):
    """kinds: list of kind names, row-major; contexts: list of 1 or 4;
    dyn_slots: 0 (every selector per context) or D dynamic slots per
    contexted cell (SLOT); actor: logic cells also offer the control-actor
    mode (ACTOR; eight sinks, LUTs still read six)."""
    if len(kinds) != rows * cols or len(contexts) != rows * cols:
        raise ValueError('one kind and one context count per cell')
    if kinds[word_port] != 'crc':
        raise ValueError('the word port must be a crc cell')
    if any(c not in (1, 4) for c in contexts):
        raise ValueError('contexts are 1 or 4')
    if any(k == 'logic' and c != 1 for k, c in zip(kinds, contexts)):
        raise ValueError('a logic cell has one context (its LUTs use the other contexts\' bits)')
    if not 0 <= dyn_slots <= (192 - SLOT['lsb']) // SLOT['bits']:
        raise ValueError(f'dyn_slots {dyn_slots}: 0-{(192 - SLOT["lsb"]) // SLOT["bits"]}')
    layout = dict(LAYOUT, dyn_slots=dyn_slots, slot=SLOT) if dyn_slots else LAYOUT
    if ctx_tracks != 0b1111:
        if dyn_slots:
            raise ValueError('ctx_tracks is the fixed split; dyn_slots the indexed one: not both')
        layout = dict(LAYOUT, ctx_tracks=ctx_tracks)
    kinds_ = KINDS
    if actor:
        layout = dict(layout, actor=ACTOR)
        kinds_ = dict(KINDS, logic=dict(KINDS['logic'], sinks=[f's{k}' for k in range(ACTOR['inputs'])],
                                        lut_sinks=len(KINDS['logic']['sinks'])))
    return {'schema': 'reorg.mesh.v1', 'name': name or f'mesh_{rows}x{cols}', 'rows': rows, 'cols': cols,
            'cells': [{'kind': k, 'contexts': c} for k, c in zip(kinds, contexts)],
            'word_port': {'cell': word_port, 'locals': {'avail': 1, 'match': 2, 'mismatch': 3}, 'sinks': {'gate': 5}},
            'kinds': kinds_, 'encoding': ENCODING, 'layout': layout, 'registers': REGISTERS,
            'latency': LATENCY, 'timing': TIMING, 'pins': 8}


def verilog_params(contract):
    n = contract['rows'] * contract['cols']
    code = lambda c: contract['kinds'][c['kind']]['code'] & 3 if c['kind'] not in ('counter', 'logic') else 1
    kinds = sum(code(c) << (2 * i) for i, c in enumerate(contract['cells']))
    ctx4 = sum((c['contexts'] == 4) << i for i, c in enumerate(contract['cells']))
    cnt = sum((c['kind'] == 'counter') << i for i, c in enumerate(contract['cells']))
    p = {'ROWS': contract['rows'], 'COLS': contract['cols'], 'WORD_PORT': contract['word_port']['cell'],
         'KINDS': f"{2 * n}'d{kinds}", 'CTX4': f"{n}'d{ctx4}"}
    if cnt:
        p['CNT'] = f"{n}'d{cnt}"
    logic = sum((c['kind'] == 'logic') << i for i, c in enumerate(contract['cells']))
    if logic:
        p['LOGIC'] = f"{n}'d{logic}"
    if contract['layout'].get('dyn_slots'):
        p['DSLOTS'] = contract['layout']['dyn_slots']
    if contract['layout'].get('ctx_tracks', 0b1111) != 0b1111:
        p['CTX_TRACKS'] = f"4'd{contract['layout']['ctx_tracks']}"
    if contract['layout'].get('actor'):
        p['ACTOR'] = 1
    return p


def config_bits(contract):
    lay = contract['layout']
    return contract['rows'] * contract['cols'] * lay['cell_bits'] + lay['wnet_bits']


class Grid:
    def __init__(self, contract):
        self.c = contract
        self.rows, self.cols = contract['rows'], contract['cols']
        self.n = self.rows * self.cols
        self.tracks = contract['layout']['tracks']

    def neighbour(self, cell, d):
        r, c = divmod(cell, self.cols)
        dr, dc = DELTA[d]
        r, c = r + dr, c + dc
        return r * self.cols + c if 0 <= r < self.rows and 0 <= c < self.cols else None

    def kind(self, cell):
        return self.c['cells'][cell]['kind']

    def contexts(self, cell):
        return self.c['cells'][cell]['contexts']

    def nlocals(self, cell):
        return len(self.c['kinds'][self.kind(cell)]['locals'])

    def nsinks(self, cell):
        return len(self.c['kinds'][self.kind(cell)]['sinks'])

    def pos(self, cell):
        return divmod(cell, self.cols)

    def manhattan(self, a, b):
        if a is None or b is None:
            return 0
        (ra, ca), (rb, cb) = self.pos(a), self.pos(b)
        return abs(ra - rb) + abs(ca - cb)

    def turn_ok(self, t, out_dir):
        vertical = out_dir in VERTICAL
        return (t < self.c['encoding']['turn_split']) == vertical


class RRG(Grid):
    """Nodes: ('opin', cell, k), ('track', cell, dir, t) = the wire leaving
    `cell` toward `dir`, ('ipin', cell, k), ('arrive', cell) (virtual: any
    signal present at a cell's switch, readable by its sinks and event
    rows). Edges carry the selector value at the node they enter."""

    def __init__(self, contract):
        super().__init__(contract)
        enc = contract['encoding']
        tv, sv = enc['track'], enc['sink']
        self.out, self.into = {}, {}
        nodes = []
        for cell in range(self.n):
            nodes += [('opin', cell, k) for k in range(self.nlocals(cell))]
            nodes += [('ipin', cell, k) for k in range(self.nsinks(cell))]
            nodes.append(('arrive', cell))
            for d in DIRS:
                if self.neighbour(cell, d) is not None:
                    nodes += [('track', cell, d, t) for t in range(self.tracks)]
        for node in nodes:
            self.out[node] = []
            self.into[node] = []

        def edge(a, b, value):
            self.out[a].append((b, value))
            self.into[b].append((a, value))

        for cell in range(self.n):
            for k in range(self.nlocals(cell)):
                o = ('opin', cell, k)
                for d in DIRS:
                    if self.neighbour(cell, d) is not None:
                        for t in range(self.tracks):
                            edge(o, ('track', cell, d, t), tv['local_first'] + k)
                for j in range(self.nsinks(cell)):
                    edge(o, ('ipin', cell, j), sv['local_first'] + k)
                edge(o, ('arrive', cell), None)
            # Tracks arriving at `cell` from neighbour on side `port`.
            for port in DIRS:
                prev = self.neighbour(cell, port)
                if prev is None:
                    continue
                for t in range(self.tracks):
                    a = ('track', prev, OPPOSITE[port], t)
                    for j in range(self.nsinks(cell)):
                        edge(a, ('ipin', cell, j), sv[f'{port}_first'] + t)
                    edge(a, ('arrive', cell), None)
                    for d in DIRS:
                        if self.neighbour(cell, d) is None:
                            continue
                        v = self.track_value(port, d, t)
                        if v is not None:
                            edge(a, ('track', cell, d, t), v)

    def track_value(self, port, d, t):
        """Selector value for output (d, t) reading the input on side `port`
        (same track), or None if the switch cannot connect them."""
        enc = self.c['encoding']
        if port == OPPOSITE[d]:
            return enc['track']['straight']
        if port == d:
            return None                                  # no U-turn
        if not self.turn_ok(t, d):
            return None
        sides = enc['turn_inputs']['vertical' if d in VERTICAL else 'horizontal']
        return enc['track']['turn_a'] if port == sides[0] else enc['track']['turn_b']

    def cell_of(self, node):
        kind = node[0]
        if kind == 'track':
            return self.neighbour(node[1], node[2])
        return node[1] if kind in ('opin', 'ipin', 'arrive') else None

    def owner(self, node):
        """The cell whose switch holds the node's selector."""
        return node[1] if node[0] in ('track', 'ipin') else None

    def value(self, a, b):
        for n, v in self.out[a]:
            if n == b:
                return v
        raise KeyError((a, b))


class ConfigSim(Grid):
    """Evaluate a configuration vector as the RTL does, given each cell's
    context. Independent of the emitter: decodes bit fields itself."""

    def __init__(self, contract, bits):
        super().__init__(contract)
        self.bits = bits
        lay = contract['layout']
        self.lay = lay

    def field(self, lsb, width):
        return (self.bits >> lsb) & ((1 << width) - 1)

    def slot(self, cell, ctx, index):
        """A dynamic slot's value for selector `index` in context ctx > 0,
        or None (the static value applies). The first matching slot wins."""
        lay = self.lay
        if not lay.get('dyn_slots') or ctx == 0 or self.contexts(cell) == 1:
            return None
        sl = lay['slot']
        for s in range(lay['dyn_slots']):
            base = cell * lay['cell_bits'] + lay['track_lsb'] + sl['lsb'] + sl['bits'] * s
            if self.field(base, 5) == index:
                return self.field(base + 5 * ctx, 5)
        return None

    def track_sel(self, cell, ctx, d, t):
        lay = self.lay
        if lay.get('dyn_slots') and self.contexts(cell) > 1:
            v = self.slot(cell, ctx, lay['slot']['track_first'] + 4 * DIRS.index(d) + t)
            if v is not None:
                return v & 7
            ctx = 0
        if not (lay.get('ctx_tracks', 0b1111) >> t) & 1:
            ctx = 0
        return self.field(cell * lay['cell_bits'] + lay['track_lsb'] + 48 * ctx + 12 * DIRS.index(d) + 3 * t, 3)

    def sink_sel(self, cell, ctx, k):
        lay = self.lay
        if lay.get('dyn_slots') and self.contexts(cell) > 1:
            v = self.slot(cell, ctx, lay['slot']['sink_first'] + k)
            if v is not None:
                return v
            ctx = 0
        return self.field(cell * lay['cell_bits'] + lay['sink_lsb'] + 5 * lay['sink_slots'] * ctx + 5 * k, 5)

    def trans(self, cell, ctx):
        lay = self.lay
        if self.contexts(cell) == 1:
            return (0, 0, 0, 0)
        x = self.field(cell * lay['cell_bits'] + lay['trans_lsb'] + 14 * ctx, 14)
        return (x & 31, (x >> 5) & 3, (x >> 7) & 31, (x >> 12) & 3)

    def local(self, cell, k):
        return ('opin', cell, k) if k < self.nlocals(cell) else ('const', 0)

    def track_out(self, cell, d, t, ctx, depth=0):
        """Signal on the wire leaving `cell` toward d on track t (ctx_switch pick)."""
        if depth > 4 * self.n:
            raise RuntimeError('combinational loop')
        s = self.track_sel(cell, ctx[cell], d, t)
        if 1 <= s <= 4:
            return self.local(cell, s - 1)
        if d in VERTICAL:
            straight, ta, tb, allow = OPPOSITE[d], 'west', 'east', t < 2
        else:
            straight, ta, tb, allow = OPPOSITE[d], 'north', 'south', t >= 2
        port = straight if s == 5 else ta if s == 6 and allow else tb if s == 7 and allow else None
        if port is None:
            return ('const', 0)
        return self.input(cell, port, t, ctx, depth + 1)

    def input(self, cell, port, t, ctx, depth=0):
        src = self.neighbour(cell, port)
        if src is None:
            return ('const', 0)
        return self.track_out(src, OPPOSITE[port], t, ctx, depth)

    def select(self, cell, v, ctx):
        """ctx_switch sel(): what a sink or event selector value reads."""
        if v == 0:
            return ('const', 0)
        if v == 1:
            return ('const', 1)
        if v < 6:
            return self.local(cell, v - 2)
        for i, port in enumerate(DIRS):
            if v < 10 + 4 * i:
                return self.input(cell, port, v - 6 - 4 * i, ctx)
        return ('const', 0)

    def sink(self, cell, k, ctx):
        return self.select(cell, self.sink_sel(cell, ctx[cell], k), ctx)


def load(path):
    return json.loads(Path(path).read_text())


def test_mesh():
    """The test configuration (a 3 x 5 candidate: 3 rows, 5 columns, wide
    like the 8 x 4 Tiny Tapeout die). Not the chosen fabric: the grid-size
    study decides the size and mix. test_3x5c (step 4 L3): the bottom-centre
    timer of test_3x5b is a counter, and the shifter cells have contexts
    (an FSM whose mux has a constant-1 case must sit at the cell it drives,
    e.g. a shifter's start level). Contexts on io, shifter and crc cells."""
    k = ['timer', 'io', 'timer', 'io', 'timer',
         'shifter', 'crc', 'shifter', 'crc', 'shifter',
         'timer', 'io', 'counter', 'io', 'timer']
    return make_contract(3, 5, k, [4 if x in ('io', 'crc', 'shifter') else 1 for x in k], 6, 'test_3x5c')


def logic_meshes():
    """The chips with logic cells (FABRIC_REORG.md: a little programmable
    logic beside the coarse tiles; each branch's src/project.v is one):
    3 x 5 with four timers (Ethernet's link timing), and 3 x 4 where the
    small timers are LUTs. Contexts on io, shifter and crc cells; shifters
    on both sides of the word-port crc (the I2C machines route only so)."""
    T, I, S, C, K, L = 'timer', 'io', 'shifter', 'crc', 'counter', 'logic'

    def mesh(name, rows, cols, kinds, wp):
        return make_contract(rows, cols, kinds, [4 if k in (I, S, C) else 1 for k in kinds], wp, name)
    k35 = [T, L, I, L, T, S, C, S, C, I, T, K, L, K, T]
    return [mesh('mesh_3x5_L3', 3, 5, k35, 6),
            mesh('mesh_3x4_L3', 3, 4, [T, L, I, L, S, C, S, I, T, K, L, K], 5),
            # The area-reduced 3 x 5 (FABRIC_REORG.md O3, single protocol):
            # contexts only on the four cells programs switch (io (0,2),
            # the word-port crc (1,1), shifter (1,2), io (1,4); the west
            # shifter and east crc never use a second context), and only
            # tracks 0 and 2 (one per turn class) held per context.
            make_contract(3, 5, k35, [4 if i in (2, 6, 7, 9) else 1 for i in range(15)], 6, 'mesh_3x5_L3S',
                          ctx_tracks=0b0101),
            # ACTOR_FABRIC.md A2: the same tiles, every switch static; the
            # logic cells also offer the control-actor mode, so a machine is
            # one actor (a logic cell) instead of switch contexts.
            make_contract(3, 5, k35, [1] * 15, 6, 'mesh_3x5_A2', actor=True)]


if __name__ == '__main__':
    import argparse
    p = argparse.ArgumentParser(description='write the test mesh contract (3 x 5 candidate)')
    p.add_argument('output', type=Path)
    a = p.parse_args()
    a.output.write_text(json.dumps(test_mesh(), indent=1) + '\n')

