#!/usr/bin/env python3
"""R1: the routing-resource graph built from the contract, checked against
an independent word-level evaluation of the mesh and against the existing
exhaustive router."""
from grid_route import routes
from grid_rrg import RRG, WordSim, load, OPPOSITE, VERTICAL

contract = load()
net = contract['bit_network']
g = RRG(contract)
cells = contract['grid']['cells']
n_cells = len(cells)

# 1. Shape: one track node per (cell, direction with a neighbour, track);
#    every local output and sink present.
interior_sides = sum(1 for c in range(n_cells) for d in net['directions'] if g.neighbour(c, d) is not None)
kinds = {}
for node in g.nodes:
    kinds[node[0]] = kinds.get(node[0], 0) + 1
assert kinds['track'] == interior_sides * net['tracks_per_direction'], kinds
assert kinds['opin'] == sum(len(c['local_outputs']) for c in cells)
assert kinds['ipin'] == len(net['sink_cells'])

# 2. Legality by construction: no U-turn, no track change, turns only as
#    the track's class allows, a track only continues from its neighbour.
for a, b, label in g.edges:
    if a[0] == 'track' and b[0] == 'track':
        _, ca, da, ta = a
        _, cb, db, tb = b
        assert ta == tb, (a, b)
        assert g.neighbour(ca, da) == cb, (a, b)
        assert db != OPPOSITE[da], ('u-turn', a, b)
        if db != da:
            order = g.classes[ta]
            assert (db in VERTICAL) == (order == 'horizontal_then_vertical'), ('turn class', a, b)
    if b[0] == 'ipin' and a[0] == 'opin':
        assert cells[a[1]]['id'] == net['sink_cells'][b[1]]
        assert not (b[1] in net['lut_sinks'] and not net['lut_local_feedback'])

# 3. Every source-to-sink path in the graph, turned into words, drives that
#    sink from that source in the independent word-level evaluation; every
#    edge is exercised by some path.
used = set()
paths = 0


def walk(node, labels, edges):
    global paths
    for nxt, label in g.out[node]:
        if nxt[0] == 'ipin':
            words = RRG.words(labels + [label])
            got = WordSim(contract, words).drives(nxt[1])
            assert got == start, (start, nxt, got, labels + [label])
            used.update(edges + [(node, nxt, label)])
            paths += 1
        elif nxt[0] == 'track':
            walk(nxt, labels + [label], edges + [(node, nxt, label)])


for start in [n for n in g.nodes if n[0] == 'opin']:
    walk(start, [], [])
live = g.live_edges()
assert used == live, (len(used), len(live))

# 4. Reachability agrees with the existing exhaustive router for every
#    (local output, sink) pair on an empty fabric, and the old router's own
#    words drive the right sink in the word-level evaluation.
by_cell = {}
for name, r in contract['resources'].items():
    by_cell.setdefault(r['cell'], name)
reach = {}
for start in [n for n in g.nodes if n[0] == 'opin']:
    seen, stack = set(), [start]
    while stack:
        x = stack.pop()
        for y, _ in g.out[x]:
            if y not in seen:
                seen.add(y)
                stack.append(y)
    reach[start] = {y[1] for y in seen if y[0] == 'ipin'}
DIRS = net['directions']
agree = 0
for start, sinks in reach.items():
    _, cell, k = start
    for s, scell in enumerate(net['sink_cells']):
        if cell not in by_cell or scell not in by_cell:
            continue
        work = {'name': 'pair', 'nets': [{'name': 'n', 'source': {'resource': by_cell[cell], 'port': k},
                                          'sinks': [{'resource': by_cell[scell], 'port': s}]}]}
        result = routes(contract, work)
        assert (result['status'] == 'routed') == (s in sinks), (start, s, result['status'])
        if result['status'] == 'routed':
            route = result['routes'][0]
            labels = [g.switch_label(h['from'], DIRS[h['direction']], route['track'], h['selector']) for h in route['hops']]
            labels.append(g.sink_label(s, route['sink_select']))
            assert WordSim(contract, RRG.words(labels)).drives(s) == start, (start, s, route)
        agree += 1

# 5. Conflicting fields are refused; hypothetical track counts build.
try:
    RRG.words([('switch', 5, 2, 0, 1), ('switch', 5, 2, 0, 5)])
except ValueError:
    pass
else:
    raise AssertionError('field conflict accepted')
for w in (2, 3, 6, 8):
    h = RRG(contract, tracks=w)
    assert sum(1 for x in h.nodes if x[0] == 'track') == interior_sides * w
    assert h.classes.count('horizontal_then_vertical') == (w + 1) // 2

print(f'PASS: RRG {len(g.nodes)} nodes / {len(g.edges)} edges from the contract; all {paths} source-to-sink '
      f'paths drive their sink in the word-level evaluation, every live edge covered ({len(g.edges) - len(live)} dead); reachability agrees with '
      f'the exhaustive router on {agree} pairs and its words check; hypothetical widths build')
