#!/usr/bin/env python3
"""R4: annealing placement + PathFinder, against brute force over every
binding (4 timers x 2 shifters x 4 LUT4 registers: at most 1152)."""
import itertools
import sys
import random
import time

from grid_pathfinder import route
from grid_place import Problem, instances, place_and_route
from grid_rrg import RRG, load

contract = load()
inst = instances(contract)

# Malformed netlists are refused.
for blocks, nets, why in [({'t': 'nosuch'}, [], 'no instance'),
                          ({f't{i}': 'timer' for i in range(5)}, [], '5 blocks'),
                          ({'t': 'timer', 'i': 'input_io'}, [{'name': 'n', 'source': ('i', 9), 'sinks': [('t', 0)]}], 'no output'),
                          ({'t': 'timer', 'i': 'input_io'}, [{'name': 'n', 'source': ('i', 0), 'sinks': [('t', 7)]}], 'no input')]:
    try:
        Problem(contract, blocks, nets)
    except ValueError as e:
        assert why in str(e), (why, e)
    else:
        raise AssertionError(why)


def random_netlist(rng):
    blocks = {'in': 'input_io', 'out': 'output_io'}
    for kind, most in (('timer', 4), ('shifter', 2), ('lut4_register', 4)):
        for i in range(rng.randint(0, most)):
            blocks[f'{kind}{i}'] = kind
    sources = [(b, p) for b, k in blocks.items() for p in range(inst[k][0][3])]
    sinks = [(b, p) for b, k in blocks.items() for p in range(len(inst[k][0][2]))]
    rng.shuffle(sinks)
    nets = []
    for i, src in enumerate(rng.sample(sources, min(len(sources), rng.randint(2, 7)))):
        want = rng.randint(1, 3)
        take = [s for s in sinks if s[0] != src[0]][:want]
        sinks = [s for s in sinks if s not in take]
        if take:
            nets.append({'name': f'n{i}', 'source': src, 'sinks': take})
    return blocks, nets


def brute(problem, rrg):
    """Every binding: (routable at all, minimum wirelength, minimum
    wirelength among routable bindings)."""
    kinds = {}
    for b, k in problem.blocks.items():
        kinds.setdefault(k, []).append(b)
    choices = [[dict(zip(bs, perm)) for perm in itertools.permutations(range(len(inst[k])), len(bs))]
               for k, bs in kinds.items()]
    ones = [1.0] * len(problem.nets)
    best_any, best_routed, any_routed = None, None, False
    by_cost = []
    for combo in itertools.product(*choices):
        binding = {}
        for c in combo:
            binding.update(c)
        by_cost.append((problem.cost(binding, ones), binding))
    by_cost.sort(key=lambda x: x[0])
    best_any = by_cost[0][0]
    for cost, binding in by_cost:   # cheapest first: the first that routes is the routable optimum
        if route(rrg, problem.routing_nets(binding))['status'] == 'routed':
            return True, best_any, cost
    return False, best_any, None


rng = random.Random(11)
stats = {'cases': 0, 'routable': 0, 'found': 0, 'optimal': 0, 'gap': 0, 'unroutable': 0}
start = time.time()
CASES = int(sys.argv[1]) if len(sys.argv) > 1 else 45   # 240 for the full run (about 20 min)
for case in range(CASES):
    blocks, nets = random_netlist(rng)
    if not nets:
        continue
    tracks = (1, 2, 4)[case % 3]
    problem = Problem(contract, blocks, nets)
    routable, best_any, best_routed = brute(problem, RRG(contract, tracks=tracks))
    result = place_and_route(contract, blocks, nets, seed=case, tracks=tracks)
    stats['cases'] += 1
    if routable:
        stats['routable'] += 1
        assert result['status'] == 'routed', (case, tracks, blocks, nets, result)
        stats['found'] += 1
        stats['phase2'] = stats.get('phase2', 0) + (result['phase'] == 2)
        assert result['wirelength'] >= best_any
        if result['wirelength'] <= best_routed:
            stats['optimal'] += 1
        stats['gap'] = max(stats['gap'], result['wirelength'] - best_routed)
    else:
        assert result['status'] == 'unroutable', case   # a claimed route would have failed the checker
        stats['unroutable'] += 1
elapsed = time.time() - start
print(f'PASS: {stats["cases"]} random netlists at 1, 2 and 4 tracks: placement routed all {stats["routable"]} that any binding routes '
      f'({stats["optimal"]} at the minimum routable wirelength, worst gap {stats["gap"]}), '
      f'{stats["unroutable"]} unroutable under every binding; {stats.get("phase2", 0)} needed the router-scored phase; {elapsed:.1f} s')
