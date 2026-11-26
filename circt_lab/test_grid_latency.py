#!/usr/bin/env python3
"""R5: route latency as a constraint. A registered hop costs a cycle; a
sink may require an exact latency or a bound. Oracle: every path of the
routing-resource graph, enumerated with its latency."""
import random

from grid_pathfinder import route, check
from grid_route import routes
from grid_rrg import RRG, WordSim, load

contract = load()
g = RRG(contract)
cols = contract['grid']['columns']


def hops_registered(tree, registered):
    return sum(1 for _, b, _ in tree if b[0] == 'track' and
               (registered is True or (b[1], g.neighbour(b[1], b[2])) in registered))


# 1. Turn class chosen by the constraint: input_io (cell 0) to the LUT's
#    input at cell 4, diagonal. East-then-south passes link 0->1, south-
#    then-east passes 0->3. Register only 0->1.
src, sink = ('opin', 0, 0), ('ipin', 10)
net = [{'name': 'a', 'source': src, 'sinks': [sink]}]
reg = {(0, 1)}
for n, track_class in ((0, 'vertical_then_horizontal'), (1, 'horizontal_then_vertical')):
    r = route(g, net, registered=reg, latency={sink: ('exact', n)})
    assert r['status'] == 'routed' and r['latency'][sink] == n, (n, r['status'])
    tracks = {b[3] for _, b, _ in r['trees']['a'] if b[0] == 'track'}
    assert all(g.classes[t] == track_class for t in tracks), (n, tracks)
    assert hops_registered(r['trees']['a'], reg) == n
    assert check(contract, net, r['words']) == []
assert route(g, net, registered=reg, latency={sink: ('exact', 2)})['status'] == 'unroutable'
assert route(g, net, registered=True, latency={sink: ('max', 1)})['status'] == 'unroutable'   # 2 hops, both registered
r = route(RRG(contract, tracks=1), net, registered=reg, latency={sink: ('max', 0)})
assert r['status'] == 'unroutable'   # one track: horizontal first only, through the register

# 2. All hops registered: the latency is the hop count, as the exhaustive
#    router computes it, for every (source, sink) pair it routes.
by_cell = {}
for name, res in contract['resources'].items():
    by_cell.setdefault(res['cell'], name)
pairs = 0
for o in [x for x in g.nodes if x[0] == 'opin' and x[1] in by_cell]:
    for s, (scell, _) in g.sinks.items():
        if scell not in by_cell or s >= 77:
            continue
        work = {'name': 'p', 'nets': [{'name': 'n', 'source': {'resource': by_cell[o[1]], 'port': o[2]},
                                       'sinks': [{'resource': by_cell[scell], 'port': s}]}]}
        old = routes(contract, work, registered=True)
        new = route(g, [{'name': 'n', 'source': o, 'sinks': [('ipin', s)]}], registered=True)
        assert (old['status'] == 'routed') == (new['status'] == 'routed'), (o, s)
        if new['status'] == 'routed':
            assert new['latency'][('ipin', s)] == old['max_route_cycles'], (o, s)
            pairs += 1

# 3. Random registered-link sets: exact n routes iff some path has latency
#    n; max n iff some path has latency <= n.
links = [(c, g.neighbour(c, d)) for c in range(len(contract['grid']['cells'])) for d in g.dirs
         if g.neighbour(c, d) is not None]


def path_latencies(source, sink, registered):
    out = set()

    def walk(node, cyc):
        for nxt, _ in g.out[node]:
            if nxt == sink:
                out.add(cyc)
            elif nxt[0] == 'track':
                walk(nxt, cyc + (1 if (nxt[1], g.neighbour(nxt[1], nxt[2])) in registered else 0))
    walk(source, 0)
    return out


rng = random.Random(9)
checked = 0
opins = [x for x in g.nodes if x[0] == 'opin']
for case in range(300):
    registered = set(rng.sample(links, rng.randint(1, len(links) // 2)))
    o = rng.choice(opins)
    s = rng.choice(list(g.sinks))
    lat = path_latencies(o, ('ipin', s), registered)
    if not lat:
        continue
    one = [{'name': 'n', 'source': o, 'sinks': [('ipin', s)]}]
    for n in range(0, max(lat) + 2):
        r = route(g, one, registered=registered, latency={('ipin', s): ('exact', n)})
        assert (r['status'] == 'routed') == (n in lat), (case, n, lat, r['status'])
        if r['status'] == 'routed':
            assert r['latency'][('ipin', s)] == n == hops_registered(r['trees']['n'], registered)
            assert WordSim(contract, r['words']).drives(s) == o
        r = route(g, one, registered=registered, latency={('ipin', s): ('max', n)})
        assert (r['status'] == 'routed') == (min(lat) <= n), (case, n, lat)
        checked += 1

print(f'PASS: an exact-latency constraint picks the turn class through or around a register; impossible '
      f"bounds are refused; all-registered latency equals the exhaustive router's on {pairs} pairs; "
      f'{checked} exact/max constraints on random registered-link sets agree with path enumeration')
