#!/usr/bin/env python3
"""Compare bounded, source-checked bit-routing slices across grid candidates."""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile

from grid_contract import DEFAULT
from grid_route import routes, mlir_problem
from grid_workload import from_report

HERE = Path(__file__).resolve().parent


def run(*args):
    return subprocess.run([str(x) for x in args], text=True, capture_output=True, check=True).stdout


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--frontend', required=True)
    p.add_argument('--mapper', required=True)
    p.add_argument('--contract', type=Path, default=DEFAULT)
    p.add_argument('--area', type=Path, required=True)
    p.add_argument('--output', type=Path, required=True)
    p.add_argument('--summary', type=Path)
    p.add_argument('--corpus-evidence', type=Path)
    p.add_argument('--physical-reports', type=Path, default=DEFAULT.parents[1] / 'experiments/grid_fabric/reports')
    args = p.parse_args()
    contract = json.loads(args.contract.read_text())
    area = json.loads(args.area.read_text())
    names = ['spi', 'uart', 'i2c', 'jtag', 'usb', 'ethernet_tx']
    workloads = {}
    with tempfile.TemporaryDirectory(prefix='grid-corpus-') as tmp:
        temp = Path(tmp)
        for name in ('spi', 'uart'):
            source = HERE / 'examples/memory_spi.spt' if name == 'spi' else HERE.parent / 'examples/uart.spt'
            front = temp / (name + '.mlir'); front.write_text(run(args.frontend, 'frontend-mlir', source))
            report = json.loads(run(args.mapper, '--compile-grid-' + name, front, temp / (name + '.mem')))
            workloads[name] = from_report(contract, report)
        for name in ('i2c', 'jtag', 'usb', 'ethernet_tx'):
            workloads[name] = json.loads(run(sys.executable, HERE / 'grid_corpus.py', name,
                                             '--frontend', args.frontend, '--mapper', args.mapper))
    centre = {'timer0': 4, 'lut': 2}
    directs = [
        {'source': {'resource':'timer0', 'port':2}, 'sink': {'resource':'shifter0', 'port':4}},
        {'source': {'resource':'timer0', 'port':1}, 'sink': {'resource':'shifter0', 'port':5}},
    ]
    candidates = [
        {'name':'current_w4', 'tracks':4},
        {'name':'current_w2_balanced', 'tracks':2},
        {'name':'current_w3_balanced', 'tracks':3},
        {'name':'current_w6_balanced', 'tracks':6},
        {'name':'centre_timer_w4', 'tracks':4, 'layout':centre},
        {'name':'centre_timer_w3', 'tracks':3, 'layout':centre},
        {'name':'centre_timer_w4_direct', 'tracks':4, 'layout':centre, 'direct':directs},
        {'name':'centre_timer_w4_selective_registers', 'tracks':4, 'layout':centre,
         'registered':[[4,3],[4,7],[4,1],[3,4],[7,4],[1,4]]},
        {'name':'current_w4_registered', 'tracks':4, 'registered':True},
    ]
    measurements = {(x['tracks_per_direction'], x['registered_outputs']):x for x in area['measurements']}
    results = []
    for candidate in candidates:
        paths = {}
        for name in names:
            oracle = routes(contract, workloads[name], candidate['tracks'], candidate.get('layout'),
                            candidate.get('registered', False), direct=candidate.get('direct'))
            if candidate.get('direct'):
                r = oracle
                r['engine'] = 'python-direct-link-prototype'
            else:
                with tempfile.TemporaryDirectory(prefix='grid-route-ir-') as tmp:
                    problem = Path(tmp) / 'route.mlir'
                    problem.write_text(mlir_problem(contract, workloads[name], candidate['tracks'], candidate.get('layout'), candidate.get('registered', False)))
                    r = json.loads(run(args.mapper, '--route-grid', problem))
                if r['status'] != oracle['status']:
                    raise ValueError(f'Python/CIRCT routing disagreement for {candidate["name"]}, {name}')
                if r['status'] == 'routed' and r['max_route_cycles'] != oracle['max_route_cycles']:
                    raise ValueError(f'Python/CIRCT cycle disagreement for {candidate["name"]}, {name}')
            paths[name] = {k:r[k] for k in ('status', 'search_nodes', 'max_hops', 'max_route_cycles',
                                             'used_directed_track_segments', 'route_count')}
            paths[name]['engine'] = r['engine']
            if r['status'] == 'routed':
                paths[name]['routes'] = r['routes']
        row = dict(candidate)
        # The per-switch proxy applies exactly only to the unmodified switch
        # logic; relocation does not change that logic. Direct muxes and
        # selective registers are not included in this proxy.
        reg = candidate.get('registered') is True
        row['isolated_switch_proxy'] = measurements.get((candidate['tracks'], reg))
        row['rtl_matches_current'] = candidate['name'] == 'current_w4'
        physical = {'current_w4':'baseline_9175808.json', 'centre_timer_w4':'centre_timer_82a420d.json'}.get(candidate['name'])
        report_path = args.physical_reports / physical if physical else None
        row['physical_layout_verified'] = bool(report_path and report_path.exists())
        if report_path and report_path.exists():
            row['physical_evidence'] = json.loads(report_path.read_text())
        row['workloads'] = paths
        row['model_objectives'] = {
            'selected_slices_routed': sum(x['status'] == 'routed' for x in paths.values()),
            'selected_slices_total': len(paths),
            'sum_used_segments': sum(x['used_directed_track_segments'] for x in paths.values() if x['status'] == 'routed'),
            'max_hops': max((x['max_hops'] for x in paths.values() if x['status'] == 'routed'), default=None),
            'max_added_cycles': max((x['max_route_cycles'] for x in paths.values() if x['status'] == 'routed'), default=None),
            'note': 'Compare segment totals only when all selected slices route; not protocol coverage, timing closure, or a physical fitness score.'
        }
        results.append(row)
    report = {'schema':'spatial.grid-candidate-sweep.v1',
              'contract':str(args.contract), 'area_library_sha256':area['liberty_sha256'],
              'contract_sha256':hashlib.sha256(args.contract.read_bytes()).hexdigest(),
              'source_sha256':{name:hashlib.sha256((HERE / 'examples/memory_spi.spt' if name == 'spi' else HERE.parent / 'examples' / (name + '.spt')).read_bytes()).hexdigest() for name in names},
              'protocol_workloads':{k:{x:v for x,v in w.items() if x != 'nets'} for k,w in workloads.items()},
              'candidates':results,
              'limitations':['These are restricted component or direct-connection slices; no full protocol fit is established.',
                             'Only explicit fixed placements and the router\'s single-track Manhattan paths are searched; failure is not proof of hardware infeasibility.',
                             'Switch area is an isolated synthesis proxy; direct muxes and selective registers, configuration, memories, physical wiring, timing and pad effects are excluded.',
                             'Registered-path cycles are arithmetic hop counts without skew compensation or a functional test.']}
    if args.corpus_evidence:
        corpus = json.loads(args.corpus_evidence.read_text())['programs']
        report['source_corpus'] = {'programs':len(corpus),
            'names':[Path(x['program']).name for x in corpus],
            'invalid_or_unresolved_connections':sum(sum(v for k,v in x['connection_status_counts'].items() if k != 'valid') for x in corpus),
            'scope':'Rust frontend and CIRCT source evidence only; full programs are not mapped.',
            'inventory':[{ 'program':Path(x['program']).name,
                           'declared_resources':x['source_resource_counts'],
                           'register_expressions':len({e['consumer'] for e in x['connections'] if e['port'].startswith('register.')}),
                           'connections':len(x['connections']),
                           'context_connections':sum(e['context'] is not None for e in x['connections']),
                           'full_grid_compiled':False,
                           'note':'Source inventory only; temporal reuse, lowering, and hardware feasibility are unresolved.'}
                          for x in corpus]}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + '\n')
    if args.summary:
        summary = dict(report)
        summary['candidates'] = []
        for item in results:
            row = dict(item)
            row['workloads'] = {name:{k:v for k,v in data.items() if k != 'routes'} for name,data in item['workloads'].items()}
            summary['candidates'].append(row)
        args.summary.parent.mkdir(parents=True, exist_ok=True)
        args.summary.write_text(json.dumps(summary, indent=2) + '\n')
    for row in results:
        print(row['name'] + ': ' + ' '.join(f'{name}={row["workloads"][name]["status"]}' for name in names))


if __name__ == '__main__':
    main()
