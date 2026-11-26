#!/usr/bin/env python3
"""Routing-resource graph (RRG) of the grid's bit mesh, built only from the
architecture contract (PLACE_ROUTE.md, milestone R1).

Nodes:
  ('opin', cell, k)       local output k of a cell
  ('track', cell, d, t)   the wire leaving `cell` toward direction d on
                          track t (only where that neighbour exists)
  ('ipin', s)             tile input (sink) s

Every edge carries the configuration that turns it on: a switch selector
(word, lo, hi, value) for edges into a track, a sink selector for edges
into an input. A track node has capacity one (one selector per switch
output), which is what routing negotiates over.

The switch's input table (`bit_network.switch_inputs`: straight, turn
first, turn last for each output) and turn classes come from the contract,
where the RTL repository's checker proves them against grid_switch.v.

`WordSim` is the independent check: given configuration words alone, it
evaluates the mesh the way the RTL does (the switch's pick() and the
sink's route_input()) and reports what drives each sink. It shares no code
with the graph builder.
"""
import json
from pathlib import Path

from grid_contract import DEFAULT

DELTA = {'north': (-1, 0), 'south': (1, 0), 'west': (0, -1), 'east': (0, 1)}
OPPOSITE = {'north': 'south', 'south': 'north', 'west': 'east', 'east': 'west'}
VERTICAL = {'north', 'south'}


def sinks(contract):
    """Every routable tile input: id -> (cell, selector word). The sink
    array (ids 0..n-1) and the contract's selector sinks (the CRC's inputs,
    ids = their words), all read through the same route_input selector."""
    net = contract['bit_network']
    table = {s: (c, w) for s, (c, w) in enumerate(zip(net['sink_cells'], net['sink_words']))}
    for k, v in net.get('selector_sinks', {}).items():
        if int(k) in table:
            raise ValueError(f'selector sink {k} collides with the sink array')
        table[int(k)] = (v['cell'], v['word'])
    return table


def _classes(net, tracks):
    """Turn order per track. At the contract's track count, the contract's
    classes; for a hypothetical count (the channel-width sweep), half and
    half as the existing router assumes."""
    if tracks == net['tracks_per_direction']:
        return [next(c['order'] for c in net['turn_classes'] if t in c['tracks']) for t in range(tracks)]
    return (['horizontal_then_vertical'] * ((tracks + 1) // 2) +
            ['vertical_then_horizontal'] * (tracks // 2))


def _turn_allowed(order, out_dir):
    # A horizontal-then-vertical track turns onto a vertical output only;
    # vertical-then-horizontal onto a horizontal output only (grid_switch:
    # allow_turn = t<2 for north/south outputs, t>=2 for west/east).
    return (out_dir in VERTICAL) == (order == 'horizontal_then_vertical')


class RRG:
    def __init__(self, contract, tracks=None):
        self.contract = contract
        grid, net = contract['grid'], contract['bit_network']
        self.rows, self.cols = grid['rows'], grid['columns']
        self.tracks = tracks or net['tracks_per_direction']
        if not 1 <= self.tracks <= 16:
            raise ValueError('tracks must be 1-16')
        self.native = self.tracks == net['tracks_per_direction']
        self.dirs = net['directions']
        self.classes = _classes(net, self.tracks)
        self.inputs = net['switch_inputs']
        sv, isel = net['selector_values'], net['input_selector']
        self.nodes, self.index, self.edges = [], {}, []
        self.out = {}

        def add(node):
            if node not in self.index:
                self.index[node] = len(self.nodes)
                self.nodes.append(node)
                self.out[node] = []
            return node

        def edge(a, b, label):
            self.out[a].append((b, label))
            self.edges.append((a, b, label))

        cells = grid['cells']
        for cell in cells:
            for k in range(len(cell['local_outputs'])):
                add(('opin', cell['id'], k))
            for d in self.dirs:
                if self.neighbour(cell['id'], d) is not None:
                    for t in range(self.tracks):
                        add(('track', cell['id'], d, t))
        self.sinks = sinks(contract)
        for s in self.sinks:
            add(('ipin', s))

        # Into a track: from a local output, straight on, or a turn.
        for (kind, *rest) in list(self.nodes):
            if kind != 'track':
                continue
            cell, d, t = rest
            for k in range(len(cells[cell]['local_outputs'])):
                edge(('opin', cell, k), ('track', cell, d, t), self.switch_label(cell, d, t, sv['local_first'] + k))
            straight, turn_first, turn_last = self.inputs[d]
            for port, value, is_turn in [(straight, sv['straight'], False),
                                         (turn_first, sv['turn_first'], True),
                                         (turn_last, sv['turn_last'], True)]:
                if is_turn and not _turn_allowed(self.classes[t], d):
                    continue
                # Input port p carries the track arriving from neighbour p,
                # i.e. travelling toward the opposite of p.
                prev = self.neighbour(cell, port)
                if prev is None:
                    continue
                edge(('track', prev, OPPOSITE[port], t), ('track', cell, d, t), self.switch_label(cell, d, t, value))
        # Into a sink: a local output of its cell, or a track arriving there.
        lut = set(net['lut_sinks'])
        for s, (cell, _) in self.sinks.items():
            if not (s in lut and not net['lut_local_feedback']):
                for k in range(len(cells[cell]['local_outputs'])):
                    edge(('opin', cell, k), ('ipin', s), self.sink_label(s, isel['local_first'] + k))
            for port in self.dirs:
                prev = self.neighbour(cell, port)
                if prev is None:
                    continue
                for t in range(self.tracks):
                    edge(('track', prev, OPPOSITE[port], t), ('ipin', s),
                         self.sink_label(s, self.port_first(port) + t))

    def live_edges(self):
        """Edges on some path from a local output to a sink. The rest are
        switch resources no route can use (a track into a cell with no
        inputs that cannot continue, or out of a region no output reaches):
        reported to the hardware side as dead configuration."""
        fwd = {n for n in self.nodes if n[0] == 'opin'}
        bwd = {n for n in self.nodes if n[0] == 'ipin'}
        for seen, forward in ((fwd, True), (bwd, False)):
            changed = True
            while changed:
                changed = False
                for a, b, _ in self.edges:
                    x, y = (a, b) if forward else (b, a)
                    if x in seen and y not in seen:
                        seen.add(y)
                        changed = True
        return {(a, b, l) for a, b, l in self.edges if a in fwd and b in bwd}

    def neighbour(self, cell, d):
        r, c = divmod(cell, self.cols)
        dr, dc = DELTA[d]
        r, c = r + dr, c + dc
        return r * self.cols + c if 0 <= r < self.rows and 0 <= c < self.cols else None

    def port_first(self, port):
        isel = self.contract['bit_network']['input_selector']
        if self.native:
            return isel[f'{port}_first']
        return isel['north_first'] + self.tracks * self.dirs.index(port)   # hypothetical widths

    def switch_label(self, cell, d, t, value):
        sw = self.contract['bit_network']['switch_words']
        bits = sw['selector_bits']
        if cell < sw['extension_first_cell']:
            word = sw['first'] + cell * sw['words_per_cell'] + self.dirs.index(d)
        else:
            word = sw['extension_first'] + (cell - sw['extension_first_cell']) * sw['words_per_cell'] + self.dirs.index(d)
        return ('switch', word, t * bits + bits - 1, t * bits, value)

    def sink_label(self, s, value):
        return ('sink', self.sinks[s][1], 15, 0, value)

    @staticmethod
    def words(labels, base=None):
        """Configuration words for a set of edge labels; two labels that
        need different values in the same field conflict."""
        words = dict(base or {})
        fields = {}
        for _, word, hi, lo, value in labels:
            key = (word, hi, lo)
            if fields.setdefault(key, value) != value:
                raise ValueError(f'field {key} needs {fields[key]} and {value}')
            mask = ((1 << (hi - lo + 1)) - 1) << lo
            words[word] = (words.get(word, 0) & ~mask) | (value << lo)
        return words


class WordSim:
    """Evaluate the mesh from configuration words alone, as the RTL does.

    drives(s) -> what sink s reads: ('opin', cell, k), ('const', v), or
    None (a selector that reads nothing, e.g. a boundary input)."""

    def __init__(self, contract, words):
        g, n = contract['grid'], contract['bit_network']
        self.rows, self.cols = g['rows'], g['columns']
        self.cells, self.n, self.words = g['cells'], n, words
        self.sinks = sinks(contract)

    def _word(self, w):
        return self.words.get(w, 0)

    def _switch_sel(self, cell, d, t):
        sw = self.n['switch_words']
        i = self.n['directions'].index(d)
        w = (sw['first'] + cell * sw['words_per_cell'] + i if cell < sw['extension_first_cell'] else
             sw['extension_first'] + (cell - sw['extension_first_cell']) * sw['words_per_cell'] + i)
        b = sw['selector_bits']
        return (self._word(w) >> (t * b)) & ((1 << b) - 1)

    def _from(self, cell, port):
        # n_in[x] = s_out[x - cols] etc.: the neighbour on side `port`.
        r, c = divmod(cell, self.cols)
        dr, dc = DELTA[port]
        r, c = r + dr, c + dc
        return r * self.cols + c if 0 <= r < self.rows and 0 <= c < self.cols else None

    def _local(self, cell, k):
        return ('opin', cell, k) if k < len(self.cells[cell]['local_outputs']) else None

    def output(self, cell, d, t, depth=0):
        """The signal on switch output (cell, d, t): RTL pick()."""
        if depth > 4 * self.rows * self.cols:
            raise RuntimeError('combinational loop')   # cannot happen with fixed classes
        sv = self.n['selector_values']
        s = self._switch_sel(cell, d, t)
        if sv['local_first'] <= s <= sv['local_last']:
            return self._local(cell, s - sv['local_first'])
        straight, ta, tb = self.n['switch_inputs'][d]
        order = next(c['order'] for c in self.n['turn_classes'] if t in c['tracks'])
        allow = (d in VERTICAL) == (order == 'horizontal_then_vertical')
        port = straight if s == sv['straight'] else ta if s == sv['turn_first'] and allow else \
            tb if s == sv['turn_last'] and allow else None
        if port is None:
            return None
        src = self._from(cell, port)
        return None if src is None else self.output(src, OPPOSITE[port], t, depth + 1)

    def drives(self, s):
        """RTL route_input() for sink s."""
        n = self.n
        cell, word = self.sinks[s]
        v = self._word(word)
        isel = n['input_selector']
        if v == isel['constant_zero']:
            return ('const', 0)
        if v == isel['constant_one']:
            return ('const', 1)
        if isel['local_first'] <= v < isel['north_first']:
            if s in n['lut_sinks'] and not n['lut_local_feedback']:
                return None   # the LUT's local inputs are tied to 0
            return self._local(cell, v - isel['local_first'])
        tracks = n['tracks_per_direction']
        for port in n['directions']:
            first = isel[f'{port}_first']
            if first <= v < first + tracks:
                src = self._from(cell, port)
                return None if src is None else self.output(src, OPPOSITE[port], v - first)
        return None


def load(contract_path=None):
    return json.loads(Path(contract_path or DEFAULT).read_text())


if __name__ == '__main__':
    g = RRG(load())
    kinds = {}
    for node in g.nodes:
        kinds[node[0]] = kinds.get(node[0], 0) + 1
    live = g.live_edges()
    print(f'RRG: {len(g.nodes)} nodes {kinds}, {len(g.edges)} edges ({len(g.edges) - len(live)} dead), {g.tracks} tracks')
    dead_tracks = sorted({b for a, b, _ in g.edges if b[0] == 'track'} - {b for a, b, _ in live})
    print('dead track outputs:', dead_tracks)
