#!/usr/bin/env python3
"""Simulated-annealing placement (PLACE_ROUTE.md, milestone R4).

A logical netlist names blocks of a kind (`timer`, `shifter`,
`lut4_register`, `input_io`, ...) and nets from a block's output port to
block input ports. Placement binds every block to a physical instance of
its kind from the contract; routing (grid_pathfinder) then connects the
bound ports. Instances of a kind expose the same ports in the same order
(the contract's resource tables), so a binding changes only where a port
is, never what it means.

The annealer is VPR's: random swaps between instances of a kind (or a move
to a free one), cost = sum over nets of bounding-box half-perimeter in
cells, temperature from the spread of initial move deltas, cooling set by
the acceptance rate. If the best placement does not route, a second anneal
scores each move by the router's own overuse (place_and_route).
"""
import math
import random

from grid_pathfinder import route, check
from grid_rrg import RRG


def instances(contract):
    """kind -> [(resource, cell, [input sinks], output count)] from the
    contract. A resource alone in its cell is movable among its kind: its
    inputs are its `input_sinks`, else its cell's sinks; its outputs are the
    cell's local outputs. Resources sharing a cell (memory with the
    sequencer, the CRC with the coded shifter) are fixed: each is its own
    kind `fixed:<name>`, exposing the cell's outputs by index and its own
    `input_sinks` or selector sinks."""
    net = contract['bit_network']
    cells = contract['grid']['cells']
    claimed = {s for r in contract['resources'].values() for s in r.get('input_sinks', [])}
    selector = {}
    for k, v in net.get('selector_sinks', {}).items():
        selector.setdefault(v['resource'], []).append(int(k))
    out = {}
    for name, r in contract['resources'].items():
        cell = r['cell']
        outputs = len(cells[cell]['local_outputs'])
        if len(cells[cell]['resources']) == 1:
            ins = r.get('input_sinks') or [s for s, c in enumerate(net['sink_cells']) if c == cell and s not in claimed]
            out.setdefault(r['kind'], []).append((name, cell, ins, outputs))
        else:
            ins = r.get('input_sinks') or sorted(selector.get(name, []))
            out[f'fixed:{name}'] = [(name, cell, ins, outputs)]
    return out


class Problem:
    def __init__(self, contract, blocks, nets):
        """blocks: {name: kind}; nets: [{'name', 'source': (block, port),
        'sinks': [(block, port), ...]}]."""
        self.contract = contract
        self.inst = instances(contract)
        self.blocks = dict(blocks)
        for b, kind in self.blocks.items():
            if kind not in self.inst:
                raise ValueError(f'{b}: no instance of kind {kind}')
        for kind in set(self.blocks.values()):
            need = sum(1 for k in self.blocks.values() if k == kind)
            if need > len(self.inst[kind]):
                raise ValueError(f'{need} blocks of kind {kind}, {len(self.inst[kind])} instances')
        self.nets = nets
        for n in nets:
            b, p = n['source']
            if p >= self.inst[self.blocks[b]][0][3]:
                raise ValueError(f'{n["name"]}: {b} has no output {p}')
            for b, p in n['sinks']:
                if p >= len(self.inst[self.blocks[b]][0][2]):
                    raise ValueError(f'{n["name"]}: {b} has no input {p}')
        self.cols = contract['grid']['columns']

    def cell(self, binding, block):
        return self.inst[self.blocks[block]][binding[block]][1]

    def net_cost(self, binding, n):
        pts = [self.cell(binding, n['source'][0])] + [self.cell(binding, b) for b, _ in n['sinks']]
        rows = [p // self.cols for p in pts]
        cols = [p % self.cols for p in pts]
        return (max(rows) - min(rows)) + (max(cols) - min(cols))

    def cost(self, binding, weights):
        return sum(weights[i] * self.net_cost(binding, n) for i, n in enumerate(self.nets))

    def verify(self, binding):
        """Placement rules beyond kind matching; none for the grid."""

    def routing_nets(self, binding):
        out = []
        for n in self.nets:
            b, p = n['source']
            _, cell, _, _ = self.inst[self.blocks[b]][binding[b]]
            sinks = [('ipin', self.inst[self.blocks[b2]][binding[b2]][2][p2]) for b2, p2 in n['sinks']]
            out.append({'name': n['name'], 'source': ('opin', cell, p), 'sinks': sinks})
        return out


def anneal(problem, rng, weights=None, binding=None, cost_fn=None, stop=None):
    weights = weights or [1.0] * len(problem.nets)
    cost_fn = cost_fn or (lambda b: problem.cost(b, weights))
    by_kind = {}
    for b, k in problem.blocks.items():
        by_kind.setdefault(k, []).append(b)
    if binding is None:
        binding = {}
        for k, bs in by_kind.items():
            slots = rng.sample(range(len(problem.inst[k])), len(bs))
            binding.update(zip(bs, slots))
    movable = [b for b, k in problem.blocks.items() if len(problem.inst[k]) > 1]
    if not movable:
        return binding, cost_fn(binding)

    def move(bind):
        b = rng.choice(movable)
        k = problem.blocks[b]
        slot = rng.randrange(len(problem.inst[k]))
        other = next((x for x in by_kind[k] if bind[x] == slot), None)
        new = dict(bind)
        new[b] = slot
        if other is not None:
            new[other] = bind[b]
        return new

    cost = cost_fn(binding)
    deltas = []
    for _ in range(max(20, len(movable) * 4)):
        deltas.append(cost_fn(move(binding)) - cost)
    mean = sum(deltas) / len(deltas)
    temp = 20 * math.sqrt(sum((d - mean) ** 2 for d in deltas) / len(deltas)) or 1.0
    per_temp = max(10, int(10 * len(movable) ** (4 / 3)))
    best, best_cost = dict(binding), cost
    while temp > 0.005 * max(cost, 1) / max(len(problem.nets), 1):
        accepted = 0
        for _ in range(per_temp):
            cand = move(binding)
            c = cost_fn(cand)
            if c <= cost or rng.random() < math.exp((cost - c) / temp):
                binding, cost = cand, c
                accepted += 1
                if c < best_cost:
                    best, best_cost = dict(cand), c
                    if stop is not None and c < stop:
                        return best, best_cost
        rate = accepted / per_temp
        temp *= 0.5 if rate > 0.96 else 0.9 if rate > 0.8 else 0.95 if rate > 0.15 else 0.8
    return best, best_cost


def _route_cost(problem, rrg, binding, budget):
    """Routability of a binding: 0 when it routes, else the overuse left
    after a short negotiation (unreachable sinks cost the most)."""
    result = route(rrg, problem.routing_nets(binding), max_iterations=budget)
    if result['status'] == 'routed':
        return 0, result
    if result.get('unreachable'):
        return 1000, result
    return len(result['overused']), result


def place_and_route(contract, blocks, nets, seed=1, tracks=None, budget=8, problem=None, rrg=None):
    """Two phases, as VPR's routability-driven placement but with the
    router itself as the estimate (routing a netlist takes milliseconds):
    1. anneal on bounding-box wirelength; route; done if it routes.
    2. otherwise anneal on (router overuse, wirelength): every move is
       scored by a short PathFinder run, so placement sees what routing
       sees: track scarcity and turn classes, not just distance.
    The result is always checked from its words (R2)."""
    problem = problem or Problem(contract, blocks, nets)
    rng = random.Random(seed)
    rrg = rrg or RRG(contract, tracks=tracks)
    ones = [1.0] * len(nets)

    def finish(binding, result, phase):
        rnets = problem.routing_nets(binding)
        if getattr(rrg, 'native', False):   # words exist only at the contract's track count
            problems = check(contract, rnets, result['words'])
            assert not problems, problems
        problem.verify(binding)
        return {'status': 'routed', 'phase': phase,
                'binding': {b: problem.inst[problem.blocks[b]][s][0] for b, s in binding.items()},
                'wirelength': problem.cost(binding, ones), 'iterations': result['iterations'],
                'words': result['words'], 'routing_nets': rnets}

    binding, _ = anneal(problem, rng)
    result = route(rrg, problem.routing_nets(binding))
    if result['status'] == 'routed':
        return finish(binding, result, 1)
    scale = max(1, sum(problem.net_cost(binding, n) for n in nets))

    def cost(b):
        over, res = _route_cost(problem, rrg, b, budget)
        cache[tuple(sorted(b.items()))] = res
        return over * 10 * scale + problem.cost(b, ones)

    cache = {}
    binding, best = anneal(problem, rng, binding=binding, cost_fn=cost, stop=10 * scale)
    if best < 10 * scale:   # zero overuse: routed within the budget
        res = cache[tuple(sorted(binding.items()))]
        if res['status'] != 'routed':
            res = route(rrg, problem.routing_nets(binding))
        if res['status'] == 'routed':
            return finish(binding, res, 2)
    return {'status': 'unroutable', 'best_overuse': best // (10 * scale)}
