#!/usr/bin/env python3
"""Check router legality, contention, fanout and honest bounded-search status."""
import argparse
import copy
import json
import random
import subprocess
import tempfile
from pathlib import Path

from grid_contract import DEFAULT
from grid_route import routes, mlir_problem
from grid_workload import from_report

parser = argparse.ArgumentParser()
parser.add_argument('--mapper', required=True)
parser.add_argument('--circt-opt', required=True)
parser.add_argument('--dialect', required=True)
parser.add_argument('reports', nargs='*')
args = parser.parse_args()
c = json.loads(DEFAULT.read_text())


def circt(workload, count=4, layout=None, registered=False, limit=1000000):
    with tempfile.TemporaryDirectory(prefix='grid-circt-route-') as tmp:
        path = Path(tmp) / 'problem.mlir'
        path.write_text(mlir_problem(c, workload, tracks=count, layout=layout, registered=registered, limit=limit))
        subprocess.run([args.circt_opt, '--load-dialect-plugin=' + args.dialect, path, '-o', '/dev/null'], check=True, capture_output=True)
        report = subprocess.run([args.mapper, '--route-grid', path], check=True, text=True, capture_output=True)
        return json.loads(report.stdout)


def net(name, port, sinks):
    return {'name': name, 'source': {'resource': 'input_io', 'port': port},
            'sinks': [{'resource': 'timer0', 'port': sink} for sink in sinks]}


one = {'name': 'fanout', 'nets': [net('a', 0, [0, 1])]}
r = routes(c, one, tracks=1)
assert r['status'] == 'routed' and r['used_directed_track_segments'] == 2
assert len(r['routes']) == 2 and all(p['track'] == 0 for p in r['routes'])
two = {'name': 'contention', 'nets': [net('a', 0, [0]), net('b', 1, [1])]}
assert routes(c, two, tracks=1)['status'] == 'unroutable_in_model'
assert routes(c, two, tracks=2)['status'] == 'routed'
for workload in (one, two):
    for count in (1, 2, 4):
        assert circt(workload, count)['status'] == routes(c, workload, count)['status']
rng = random.Random(342)
destinations = [('timer0', 0), ('timer0', 1), ('shifter0', 3), ('shifter0', 4), ('output_io', 7)]
for case in range(12):
    picks = rng.sample(destinations, 3)
    workload = {'name':f'seeded-{case}', 'nets':[
        {'name':'a', 'source':{'resource':'input_io', 'port':0},
         'sinks':[{'resource':r, 'port':p} for r,p in picks[:2]]},
        {'name':'b', 'source':{'resource':'input_io', 'port':1},
         'sinks':[{'resource':picks[2][0], 'port':picks[2][1]}]}]}
    for count in (1, 2, 4):
        assert circt(workload, count)['status'] == routes(c, workload, count)['status']
assert routes(c, two, tracks=2, limit=1)['status'] == 'unknown_search_limit'
assert circt(two, count=2, limit=1)['status'] == 'unknown_search_limit'
partial = routes(c, one, tracks=1, registered=[[0, 1]])
assert partial['max_route_cycles'] == 1
assert routes(c, one, tracks=1, registered=True)['max_route_cycles'] == 2
assert circt(one, count=1, registered=[[0, 1]])['max_route_cycles'] == 1
assert circt(one, count=1, registered=True)['max_route_cycles'] == 2
direct = [{'source': {'resource':'input_io', 'port':0}, 'sink': {'resource':'timer0', 'port':0}}]
assert routes(c, two, tracks=1, direct=direct)['status'] == 'routed'
bad = copy.deepcopy(two); bad['nets'][1]['sinks'][0]['port'] = 0
try:
    routes(c, bad)
    raise AssertionError('accepted two sources driving one sink')
except ValueError:
    pass
bad = copy.deepcopy(two); bad['nets'][1]['source']['port'] = 4
try:
    routes(c, bad)
    raise AssertionError('accepted nonexistent source port')
except ValueError:
    pass
for bad_layout in ({'timer0':4}, {'crc':2}):
    try:
        routes(c, one, layout=bad_layout)
        raise AssertionError('accepted overlapping or split cell placement')
    except ValueError:
        pass

for filename in args.reports:
    report = json.loads(Path(filename).read_text())
    workload = from_report(c, report)
    result = routes(c, workload)
    compiled = circt(workload)
    assert compiled['engine'] == 'circt-spatial-grid-route-v1' and compiled['status'] == result['status']
    center = circt(workload, layout={'timer0':4, 'lut':2})
    assert center['status'] == 'routed' and center['max_hops'] <= compiled['max_hops']
    assert result['status'] == 'routed'
    assert result['route_count'] == sum(len(n['sinks']) for n in workload['nets'])
    owners = {}
    for p in result['routes']:
        for h in p['hops']:
            key = h['from'], h['direction'], p['track']
            assert key not in owners or owners[key] == (p['net'], h['selector'])
            owners[key] = p['net'], h['selector']
        assert all(a['to'] == b['from'] for a, b in zip(p['hops'], p['hops'][1:]))
with tempfile.TemporaryDirectory(prefix='grid-bad-ir-') as tmp:
    problem = Path(tmp) / 'bad.mlir'
    problem.write_text(mlir_problem(c, one).replace('horizontal_then_vertical', 'invalid_turn_class'))
    assert subprocess.run([args.mapper, '--route-grid', problem], capture_output=True).returncode != 0
    problem.write_text(mlir_problem(c, one).replace('0 : i64, 1 : i64, 5 : i64, 6 : i64, 7 : i64', '0 : i64'))
    assert subprocess.run([args.mapper, '--route-grid', problem], capture_output=True).returncode != 0
print('PASS: CIRCT route parity, contention, shared fanout, invalid endpoints and bounded-search status')
