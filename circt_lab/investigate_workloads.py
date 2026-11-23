#!/usr/bin/env python3
"""Extract checked source connectivity for architecture investigation.

Preserves unresolved facts and source expressions. This is not lowering,
placement, a concurrency proof, an image, or an RTL capability report.
"""
import argparse
from collections import Counter
import json
from pathlib import Path
import subprocess
import sys


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--frontend', required=True)
    p.add_argument('--mapper', required=True)
    p.add_argument('programs', nargs='+', type=Path)
    args = p.parse_args()
    reports = []
    for program in args.programs:
        try:
            # Read only verified MLIR via the CIRCT tool. Use a temporary file
            # because the mapper's frontend-json command accepts a path.
            import tempfile
            front = subprocess.run([args.frontend, 'frontend-mlir', str(program)], text=True, capture_output=True, check=True)
            with tempfile.TemporaryDirectory(prefix='spatial-workloads-') as tmp:
                path = Path(tmp) / 'front.mlir'; path.write_text(front.stdout)
                raw = subprocess.run([args.mapper, '--frontend-json', str(path)], text=True, capture_output=True, check=True)
            model = json.loads(raw.stdout)
            items = model['items']; inputs = model['connections']['inputs']
            resources = []
            for item in items:
                if item['kind'] != 'resource':
                    continue
                resources.append({k: item.get(k) for k in ('name', 'resource_kind', 'fields', 'timings', 'payload_width', 'state_width')})
            pins = [{k: x.get(k) for k in ('name', 'mode', 'source', 'pads')} for x in items if x['kind'] == 'pin']
            functions = [{k: x.get(k) for k in ('name', 'body')} for x in items if x['kind'] == 'function']
            connections = [{k: x.get(k) for k in ('consumer', 'port', 'context', 'expression', 'references', 'check')} for x in inputs]
            for edge in connections:
                # Signal levels and events are known 1-bit values. Do not
                # invent a width for words, functions, or unresolved signals.
                for ref in edge['references']:
                    ref['bit_width'] = 1 if ref.get('signal_kind') in ('level', 'pulse') else None
            direct = {}
            for edge in connections:
                if edge['port'] not in ('shifter.start', 'shifter.shift') or edge['context'] is not None:
                    continue
                refs = edge['references']
                if edge['check']['status'] == 'valid' and len(refs) == 1:
                    ref = refs[0]
                    key = (edge['port'], ref['name'], ref['output'])
                    direct.setdefault(key, []).append(edge['consumer'])
            shared = [{'port': port, 'source': source, 'output': output, 'consumers': sorted(consumers)}
                      for (port, source, output), consumers in sorted(direct.items(), key=lambda x: str(x[0])) if len(consumers)>1]
            shifters = sorted(x['name'] for x in resources if x['resource_kind']=='shifter')
            common_shift = any(x['port']=='shifter.shift' and x['consumers']==shifters for x in shared) if len(shifters)>1 else False
            comparison = {'shifters': shifters,
                'separate_lane_hypothesis': 'Each declared shifter has its own state and event selection; full fit unproven.',
                'combined_shared_event_hypothesis': ('All declared shifters have the same direct shift source; shared event wiring is plausible if the block also supplies the required data state.' if common_shift else
                    'Not established: declared shifters do not all use the same direct shift source; context-sensitive equivalence has not been analyzed.'),
                'combined_independent_events_hypothesis': 'Could represent distinct event selections if it also provides independent state and data paths; full fit unproven.'}
            reports.append({'program': str(program), 'clock_hz': model['clock_hz'],
                'source_resource_counts': dict(Counter(x['resource_kind'] for x in resources)),
                'pins': pins, 'resources': resources, 'connections': connections,
                'functions': functions, 'shared_direct_inputs': shared, 'shift_block_comparison': comparison,
                'connection_status_counts': dict(Counter(x['check']['status'] for x in connections)),
                'limitations': ['Declarations and references are source evidence, not physical tile counts.',
                    'Function bodies and stream behavior are retained but not semantically lowered.',
                    'Shared direct sources do not prove same-cycle activity, routability or physical timing.',
                    'No capacity, selector, host bandwidth, memory mapping or RTL equivalence proof.']})
        except (OSError, subprocess.CalledProcessError, ValueError, KeyError) as exc:
            print(f'{program}: source analysis failed: {exc}', file=sys.stderr)
            return 1
    print(json.dumps({'schema':'spatial.source-workload-evidence.v1', 'programs':reports}, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
