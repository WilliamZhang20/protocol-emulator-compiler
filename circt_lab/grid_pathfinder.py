#!/usr/bin/env python3
"""PathFinder routing over the contract's routing-resource graph, and the
independent route checker (PLACE_ROUTE.md, milestones R2 and R3).

Negotiated congestion (McMurchie and Ebeling 1995, as in VPR): every
iteration rips up and reroutes every net; entering a node costs
`(base + history) * present`, where `present` grows with the node's
overuse and with the iteration (pressure), and `history` accumulates on
nodes that stay overused. A net is a tree: each sink is reached by an A*
search started from every node already in the net's tree, so fanout
shares wires. Track and sink nodes have capacity one; local outputs are
sources only.

`check` (R2) trusts nothing from the router: it rebuilds what every sink
reads from the configuration words alone (grid_rrg.WordSim) and compares
with the nets asked for.
"""
import heapq

from grid_rrg import RRG, WordSim


def _cell_of(rrg, node):
    """Where a node's signal is: a track's arrival cell, else its cell.
    A graph with nodes of its own (grid_cluster) supplies cell_of; None
    means no mesh position (inside a cluster), and the search then has no
    distance estimate there."""
    if hasattr(rrg, 'cell_of'):
        return rrg.cell_of(node)
    kind = node[0]
    if kind == 'track':
        return rrg.neighbour(node[1], node[2])
    if kind == 'opin':
        return node[1]
    return rrg.sinks[node[1]][0]


def _manhattan(rrg, a, b):
    if a is None or b is None:
        return 0
    ra, ca = divmod(a, rrg.cols)
    rb, cb = divmod(b, rrg.cols)
    return abs(ra - rb) + abs(ca - cb)


def route(rrg, nets, max_iterations=100, pres_fac=0.5, pres_mult=1.3, hist_fac=1.0,
          registered=None, latency=None):
    """nets: [{'name', 'source': ('opin', cell, k), 'sinks': [('ipin', s), ...]}].

    registered: None (every hop combinational, the contract's hop_cycles
    0), True (every hop a register) or a set of directed (cell, next cell)
    links that are registered; a route's latency is the number of
    registered hops it takes. latency: {('ipin', s): ('exact', n) |
    ('max', n)}, constraints the search must meet: the state is (node,
    cycles so far), so a sink is reached only at an allowed latency.

    Returns {'status': 'routed'|'unroutable', 'iterations', 'trees':
    {name: [(a, b, label), ...]}, 'latency': {sink: cycles}, 'overused':
    [nodes], 'words'}."""
    names = [n['name'] for n in nets]
    if len(set(names)) != len(names):
        raise ValueError('duplicate net name')
    seen_sinks = {}
    is_source = getattr(rrg, 'is_source', lambda node: node[0] == 'opin')
    is_sink = getattr(rrg, 'is_sink', lambda node: node[0] == 'ipin')
    for n in nets:
        if n['source'] not in rrg.index or not is_source(n['source']):
            raise ValueError(f'{n["name"]}: source {n["source"]} is not a local output')
        for s in n['sinks']:
            if s not in rrg.index or not is_sink(s):
                raise ValueError(f'{n["name"]}: sink {s} is not a tile input')
            if s in seen_sinks:
                raise ValueError(f'sink {s} driven by {seen_sinks[s]} and {n["name"]}')
            seen_sinks[s] = n['name']
    latency = dict(latency or {})
    for s, (kind, n) in latency.items():
        if s not in seen_sinks or kind not in ('exact', 'max') or n < 0:
            raise ValueError(f'latency constraint {s}: {kind} {n}')

    def hop_cycles(node):
        if node[0] != 'track' or not registered:
            return 0
        return 1 if registered is True or (node[1], rrg.neighbour(node[1], node[2])) in registered else 0

    def allowed(sink, cyc):
        kind, n = latency.get(sink, ('max', None))
        return n is None or (cyc == n if kind == 'exact' else cyc <= n)

    capacity = getattr(rrg, 'capacity', None) or {node: 1 for node in rrg.nodes if node[0] in ('track', 'ipin')}
    history = {node: 0.0 for node in capacity}
    occupancy = {node: 0 for node in capacity}
    trees = {n['name']: [] for n in nets}
    reached = {}

    def cost(node, pres):
        if node not in capacity:
            return 1.0
        over = max(0, occupancy[node] + 1 - capacity[node])
        return (1.0 + history[node]) * (1.0 + pres * over)

    def rip(name):
        for _, b, _ in trees[name]:
            if b in occupancy:
                occupancy[b] -= 1
        trees[name] = []

    def route_net(net, pres):
        in_tree = {net['source']: 0}          # node -> cycles from the source
        edges = []
        # Constrained sinks first (they have the fewest acceptable paths),
        # then nearest first: later sinks branch from a tree already
        # reaching toward them.
        src_cell = _cell_of(rrg, net['source'])
        order = sorted(net['sinks'], key=lambda s: (s not in latency, _manhattan(rrg, src_cell, _cell_of(rrg, s))))
        for sink in order:
            target = _cell_of(rrg, sink)
            frontier, best, parent = [], {}, {}
            for node, cyc in in_tree.items():
                best[(node, cyc)] = 0.0
                heapq.heappush(frontier, (_manhattan(rrg, _cell_of(rrg, node), target), 0.0, node, cyc))
            found = None
            while frontier:
                _, g, node, cyc = heapq.heappop(frontier)
                if g > best.get((node, cyc), float('inf')):
                    continue
                if node == sink:
                    found = (node, cyc)
                    break
                for nxt, label in rrg.out[node]:
                    if nxt in in_tree:          # one parent per node: a selector picks one input
                        continue
                    ncyc = cyc + hop_cycles(nxt)
                    if is_sink(nxt) and (nxt != sink or not allowed(sink, ncyc)):
                        continue
                    kind, bound = latency.get(sink, ('max', None))
                    if bound is not None and ncyc > bound:
                        continue                # cycles only grow along a path
                    ng = g + cost(nxt, pres)
                    if ng < best.get((nxt, ncyc), float('inf')):
                        best[(nxt, ncyc)] = ng
                        parent[(nxt, ncyc)] = ((node, cyc), label)
                        h = 0 if is_sink(nxt) else _manhattan(rrg, _cell_of(rrg, nxt), target)
                        heapq.heappush(frontier, (ng + h, ng, nxt, ncyc))
            if found is None:
                return None
            state = found
            reached[sink] = found[1]
            while state[0] not in in_tree:
                prev, label = parent[state]
                edges.append((prev[0], state[0], label))
                in_tree[state[0]] = state[1]
                state = prev
        return edges

    pres = pres_fac
    for iteration in range(1, max_iterations + 1):
        for net in nets:
            rip(net['name'])
            edges = route_net(net, pres)
            if edges is None:   # no path at all, whatever the congestion
                return {'status': 'unroutable', 'iterations': iteration, 'trees': {}, 'overused': [],
                        'unreachable': net['name'], 'words': None}
            trees[net['name']] = edges
            for _, b, _ in edges:
                if b in occupancy:
                    occupancy[b] += 1
        overused = [n for n, o in occupancy.items() if o > capacity[n]]
        if not overused:
            labels = [l for t in trees.values() for _, _, l in t]
            return {'status': 'routed', 'iterations': iteration, 'trees': trees, 'overused': [],
                    'latency': dict(reached), 'words': RRG.words(labels)}
        for n in overused:
            history[n] += hist_fac * (occupancy[n] - capacity[n])
        pres *= pres_mult
    return {'status': 'unroutable', 'iterations': max_iterations, 'trees': trees, 'overused': overused,
            'words': None}


def check(contract, nets, words):
    """R2: from the words alone, every net's sinks read its source, and no
    other sink reads any routed source. Returns a list of problems."""
    sim = WordSim(contract, words)
    problems = []
    wanted = {}
    for n in nets:
        for s in n['sinks']:
            wanted[s[1]] = n['source']
    for s in sim.sinks:
        got = sim.drives(s)
        if s in wanted:
            if got != wanted[s]:
                problems.append(f'sink {s}: reads {got}, wants {wanted[s]}')
        elif got is not None and got[0] == 'opin':
            problems.append(f'sink {s}: unrouted but reads {got}')
    return problems
