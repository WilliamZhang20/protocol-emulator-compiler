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
    """Where a node's signal is: a track's arrival cell, else its cell."""
    kind = node[0]
    if kind == 'track':
        return rrg.neighbour(node[1], node[2])
    if kind == 'opin':
        return node[1]
    return rrg.contract['bit_network']['sink_cells'][node[1]]


def _manhattan(rrg, a, b):
    ra, ca = divmod(a, rrg.cols)
    rb, cb = divmod(b, rrg.cols)
    return abs(ra - rb) + abs(ca - cb)


def route(rrg, nets, max_iterations=60, pres_fac=0.5, pres_mult=1.6, hist_fac=0.4):
    """nets: [{'name', 'source': ('opin', cell, k), 'sinks': [('ipin', s), ...]}].

    Returns {'status': 'routed'|'unroutable', 'iterations', 'trees':
    {name: [(a, b, label), ...]}, 'overused': [nodes], 'words'}."""
    names = [n['name'] for n in nets]
    if len(set(names)) != len(names):
        raise ValueError('duplicate net name')
    seen_sinks = {}
    for n in nets:
        if n['source'] not in rrg.index or n['source'][0] != 'opin':
            raise ValueError(f'{n["name"]}: source {n["source"]} is not a local output')
        for s in n['sinks']:
            if s not in rrg.index or s[0] != 'ipin':
                raise ValueError(f'{n["name"]}: sink {s} is not a tile input')
            if s in seen_sinks:
                raise ValueError(f'sink {s} driven by {seen_sinks[s]} and {n["name"]}')
            seen_sinks[s] = n['name']
    capacity = {node: 1 for node in rrg.nodes if node[0] in ('track', 'ipin')}
    history = {node: 0.0 for node in capacity}
    occupancy = {node: 0 for node in capacity}
    trees = {n['name']: [] for n in nets}

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
        in_tree = {net['source']}
        edges = []
        # Nearest sinks first: later sinks branch from a tree that already
        # reaches toward them.
        src_cell = _cell_of(rrg, net['source'])
        for sink in sorted(net['sinks'], key=lambda s: _manhattan(rrg, src_cell, _cell_of(rrg, s))):
            target = _cell_of(rrg, sink)
            frontier, best, parent = [], {}, {}
            for node in in_tree:
                best[node] = 0.0
                heapq.heappush(frontier, (_manhattan(rrg, _cell_of(rrg, node), target), 0.0, node))
            found = False
            while frontier:
                _, g, node = heapq.heappop(frontier)
                if g > best.get(node, float('inf')):
                    continue
                if node == sink:
                    found = True
                    break
                for nxt, label in rrg.out[node]:
                    if nxt in in_tree:          # one parent per node: a selector picks one input
                        continue
                    if nxt[0] == 'ipin' and nxt != sink:
                        continue
                    ng = g + cost(nxt, pres)
                    if ng < best.get(nxt, float('inf')):
                        best[nxt] = ng
                        parent[nxt] = (node, label)
                        h = 0 if nxt[0] == 'ipin' else _manhattan(rrg, _cell_of(rrg, nxt), target)
                        heapq.heappush(frontier, (ng + h, ng, nxt))
            if not found:
                return None
            node = sink
            while node not in in_tree:
                prev, label = parent[node]
                edges.append((prev, node, label))
                in_tree.add(node)
                node = prev
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
                    'words': RRG.words(labels)}
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
    for s in range(len(contract['bit_network']['sink_cells'])):
        got = sim.drives(s)
        if s in wanted:
            if got != wanted[s]:
                problems.append(f'sink {s}: reads {got}, wants {wanted[s]}')
        elif got is not None and got[0] == 'opin':
            problems.append(f'sink {s}: unrouted but reads {got}')
    return problems
