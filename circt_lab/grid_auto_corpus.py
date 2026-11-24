#!/usr/bin/env python3
"""Explore direct bit references across the whole frontend evidence corpus.

This is a deterministic first-resource placement hypothesis, not source
lowering. Every omitted edge and resource is reported; time multiplexing,
logic synthesis, stream lowering and dynamic contexts are not inferred.
"""
import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import subprocess
import tempfile

from grid_contract import DEFAULT
from grid_route import mlir_problem


def extract(program):
    assigned, unmatched = {}, Counter()
    counts = Counter()
    for item in program['resources']:
        kind = item['resource_kind']
        limit = {'timer':2, 'shifter':2, 'crc':1}.get(kind, 0)
        index = counts[kind]; counts[kind] += 1
        if index < limit:
            assigned[item['name']] = ('crc' if kind == 'crc' else kind + str(index), kind)
        else:
            unmatched['unassigned_resource_' + kind] += 1
    inputs, outputs = {}, {}
    for pin in program['pins']:
        if pin['mode'] in ('in', 'inout'):
            if len(inputs) < 2: inputs[pin['name']] = len(inputs)
            else: unmatched['input_pin_over_capacity'] += 1
        if pin['mode'] in ('out', 'inout'):
            if len(outputs) < 3: outputs[pin['name']] = len(outputs)
            else: unmatched['output_pin_over_capacity'] += 1
    sources = {}
    for name, (physical, kind) in assigned.items():
        ports = {'timer':{'tick':0,'rise':1,'fall':2,'out':3},
                 'shifter':{'busy':0,'done':1,'out':2}, 'crc':{'out':0,'ok':1}}[kind]
        for field, port in ports.items(): sources[(name, field)] = (physical, port)
    for name, port in inputs.items():
        sources[(name, None)] = ('input_io', port)
        # Edge polarity is configurable, but one pin cannot emit both polarities
        # as independent simultaneous signals. Those are screened below.
        sources[(name, 'rise')] = ('input_io', port + 2)
        sources[(name, 'fall')] = ('input_io', port + 2)
    targets = {}
    for name, (physical, kind) in assigned.items():
        if kind == 'timer':
            first = 0 if physical == 'timer0' else 12
            for i, field in enumerate(('start','stop','hold')): targets[(name, 'timer.' + field)] = (physical, first + i)
        elif kind == 'shifter':
            first = 3 if physical == 'shifter0' else 15
            for i, field in enumerate(('start','shift','sample_at','sample_data')): targets[(name, 'shifter.' + field)] = (physical, first + i)
        elif kind == 'crc':
            for i, field in enumerate(('absorb','emit','restart','data')): targets[(name, 'crc.' + field)] = ('crc', 77 + i)
    for name, port in outputs.items(): targets[(name, 'pin.drive')] = ('output_io', 7 + port)
    candidates = []
    for edge in program['connections']:
        target = targets.get((edge['consumer'], edge['port']))
        refs = edge['references']
        if edge['context'] is not None:
            unmatched['context_edge'] += 1
        elif not target:
            unmatched['unmapped_destination'] += 1
        elif edge['check']['status'] != 'valid' or edge['expression']['kind'] not in ('name','field') or len(refs) != 1:
            unmatched['non_direct_expression'] += 1
        else:
            ref = refs[0]; source = sources.get((ref['name'], ref.get('output')))
            if source is None:
                unmatched['unmapped_source'] += 1
            else:
                candidates.append((source, target, ref['name'] + ('.' + ref['output'] if ref.get('output') else '')))
    # Do not collapse different logical signals onto one physical port, and do
    # not choose between multiple drivers for a sink without a context model.
    source_names, target_names = {}, {}
    for src,dst,name in candidates:
        source_names.setdefault(src,set()).add(name); target_names.setdefault(dst,set()).add(name)
    nets = {}
    for src,dst,name in candidates:
        if len(source_names[src]) != 1 or len(target_names[dst]) != 1:
            unmatched['ambiguous_source_or_sink'] += 1
            continue
        net = nets.setdefault(name, {'name':name, 'source':{'resource':src[0], 'port':src[1]}, 'sinks':[]})
        sink = {'resource':dst[0], 'port':dst[1]}
        if sink not in net['sinks']: net['sinks'].append(sink)
    return {'name':Path(program['program']).stem + '-automatic-direct-slice', 'nets':list(nets.values()),
            'full_source_program_compiled':False, 'selected_connections':sum(len(x['sinks']) for x in nets.values()),
            'source_connections':len(program['connections']), 'omitted':dict(unmatched),
            'placement':'first declared resource of each type; not placement search'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--evidence', type=Path, required=True)
    parser.add_argument('--mapper', required=True)
    parser.add_argument('--contract', type=Path, default=DEFAULT)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    contract = json.loads(args.contract.read_text())
    programs = json.loads(args.evidence.read_text())['programs']
    variants = [('current_w2', 2, None), ('current_w3', 3, None), ('current_w4', 4, None),
                ('current_w6', 6, None), ('centre_w4', 4, {'timer0':4, 'lut':2})]
    out = []
    with tempfile.TemporaryDirectory(prefix='grid-auto-corpus-') as tmp:
        path = Path(tmp) / 'problem.mlir'
        for program in programs:
            workload = extract(program)
            results = {}
            if workload['nets']:
                for name, width, layout in variants:
                    path.write_text(mlir_problem(contract, workload, tracks=width, layout=layout, limit=10000))
                    r = subprocess.run([args.mapper, '--route-grid', path], text=True, capture_output=True, check=True)
                    mapped = json.loads(r.stdout)
                    results[name] = {k:mapped[k] for k in ('status','search_nodes','max_hops','used_directed_track_segments')}
            else:
                results = {name:{'status':'no_supported_direct_edges'} for name, _, _ in variants}
            out.append({'program':Path(program['program']).name, 'source_resources':program['source_resource_counts'],
                        'selected_connections':workload['selected_connections'], 'source_connections':workload['source_connections'],
                        'full_source_program_compiled':False, 'nets':workload['nets'],
                        'omitted':workload['omitted'], 'candidates':results})
    report = {'schema':'spatial.grid-auto-corpus.v1', 'programs':out,
              'contract_sha256':hashlib.sha256(args.contract.read_bytes()).hexdigest(),
              'evidence_sha256':hashlib.sha256(args.evidence.read_bytes()).hexdigest(),
              'limitations':['Only direct bit references with fixed first-declared resource binding are modeled.',
                             'Omitted operations, contexts, scheduling, memories, physical pin restrictions, widths and timing are not validated.',
                             'No result establishes full program fit or a fundamental hardware deficit.']}
    args.output.write_text(json.dumps(report,indent=2)+'\n')
    print(f"assessed direct slices in {sum(x['selected_connections'] > 0 for x in out)}/{len(out)} programs")


if __name__ == '__main__':
    main()
