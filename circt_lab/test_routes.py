#!/usr/bin/env python3
"""Cross-check small selector graphs against exhaustive assignments."""
import argparse
import itertools
import json
from pathlib import Path
import random
import subprocess
import tempfile


def works(graph, choices, sink, wanted):
    seen = set()
    while True:
        if sink in seen:
            return False
        seen.add(sink)
        source = graph[sink][choices[sink]]
        if source == wanted:
            return True
        if not source.startswith('route.'):
            return False
        sink = source[6:]


def brute(graph, demands):
    names = sorted(graph)
    for selected in itertools.product(*(range(len(graph[name])) for name in names)):
        choices = dict(zip(names, selected))
        if all(works(graph, choices, sink, source) for sink, source in demands.items()):
            return True
    return False


def arch_text(graph):
    routes = ', '.join('{sink = %s, sources = [%s]}' %
                       (json.dumps(sink), ', '.join(json.dumps(x) for x in sources))
                       for sink, sources in graph.items())
    return '''module {
  "spatial.fabric"() {name = "random", shift_width = 16 : i64,
    storage_words = 4 : i64, storage_word_bits = 16 : i64,
    control_states = 4 : i64, control_inputs = 2 : i64, route_cycles = 1 : i64,
    pad_max_output_hz_estimate = 66000000 : i64, pad_roundtrip_ns_estimate = 10 : i64,
    routes = [%s]} : () -> ()
}\n''' % routes


def program_text(demands):
    pairs = ', '.join('%s = %s' % (sink, json.dumps(source)) for sink, source in demands.items())
    return '''module {
  %%received = "spatial.spi"() {target = "random", width = 8 : i64,
    half_period = 8 : i64, clock_hz = 50000000 : i64, tx_value = 165 : i64,
    storage_words_required = 2 : i64, connections = {%s}} : () -> i8
}\n''' % pairs


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--mapper', required=True)
    parser.add_argument('--cases', type=int, default=30)
    args = parser.parse_args()
    if args.cases < 1:
        parser.error('--cases must be positive')
    rng = random.Random(0x51A7)
    pool = ['p0', 'p1', 'route.r0', 'route.r1', 'route.r2']
    demands = {'r0': 'p0', 'r1': 'p1'}
    totals = [0, 0]
    with tempfile.TemporaryDirectory(prefix='spatial-routing-test-') as tmp:
        arch = Path(tmp) / 'arch.mlir'
        program = Path(tmp) / 'program.mlir'
        program.write_text(program_text(demands))
        for case in range(args.cases):
            graph = {f'r{i}': rng.sample(pool, 2) for i in range(3)}
            expected = brute(graph, demands)
            totals[expected] += 1
            arch.write_text(arch_text(graph))
            result = subprocess.run([args.mapper, '--analyze', str(arch), str(program)],
                                    text=True, capture_output=True)
            if (result.returncode == 0) != expected:
                raise AssertionError(f'case {case}: graph={graph} expected={expected} stderr={result.stderr}')
            if expected:
                report = json.loads(result.stdout)
                selected = {x['sink']: x['choice'] for x in report['selected_routes']}
                if not all(works(graph, selected, sink, source) for sink, source in demands.items()):
                    raise AssertionError(f'case {case}: invalid routes {selected} for {graph}')
            elif 'no compatible selector assignment' not in result.stderr:
                raise AssertionError(f'case {case}: unexpected failure: {result.stderr}')
    if not all(totals):
        raise AssertionError(f'random sample lacks both satisfiable and unsatisfiable graphs: {totals}')
    print(f'PASS: {args.cases} selector graphs match exhaustive search ({totals[1]} fit, {totals[0]} fail)')


if __name__ == '__main__':
    main()
