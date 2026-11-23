#!/usr/bin/env python3
"""Report honest frontend and control-component coverage for a .spt corpus."""
import argparse
from collections import Counter
import hashlib
import json
import re
from pathlib import Path
import subprocess
import tempfile

HERE = Path(__file__).resolve().parent


def source_digest(paths, base):
    digest = hashlib.sha256()
    for path in sorted(paths):
        digest.update(str(path.relative_to(base)).encode())
        digest.update(b'\0')
        digest.update(path.read_bytes())
        digest.update(b'\0')
    return digest.hexdigest()


def run(argv):
    return subprocess.run(argv, text=True, capture_output=True)


def last_error(result):
    lines = [line.strip() for line in result.stderr.splitlines() if line.strip()]
    return lines[-1] if lines else f'exit {result.returncode}'


def check_control(protocol, rows, state_map):
    """Check every reachable source row against the emitted physical table."""
    lookup = {(row['from'], tuple(row['inputs'])): row for row in protocol['transitions']}
    if len(rows) != 16 or len(state_map) != protocol['states']:
        return False
    if state_map[protocol['initial_state']] != 0:
        return False
    for state, physical in enumerate(state_map):
        if physical is None:
            continue
        for first in (False, True):
            for second in (False, True):
                values = (first,) if len(protocol['inputs']) == 1 else (first, second)
                row = lookup.get((state, values))
                next_state = row['to'] if row else state
                action = row['action'] if row else None
                flags = 0 if action is None else 1 << (action - 1)
                expected = state_map[next_state] | (flags << 2)
                if rows[(physical << 2) | (int(first) << 1) | int(second)] != expected:
                    return False
    return True


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--frontend', type=Path, required=True)
    parser.add_argument('--mapper', type=Path, required=True)
    parser.add_argument('--circt-opt', type=Path, required=True)
    parser.add_argument('--dialect', type=Path, required=True)
    parser.add_argument('--examples', type=Path, default=HERE.parent / 'examples')
    parser.add_argument('--output', type=Path, help='Write JSON here instead of stdout')
    parser.add_argument('--require-full', action='store_true', help='Exit nonzero if any full program is not mapped')
    args = parser.parse_args()
    files = sorted(args.examples.glob('*.spt'))
    if not files:
        parser.error(f'no .spt files in {args.examples}')
    project = HERE.parent
    rust_sources = list((project / 'src').rglob('*.rs')) + [project / 'Cargo.toml', project / 'Cargo.lock']
    mapper_sources = [HERE / 'map.cpp', HERE / 'dialect.cpp', HERE / 'dialect.h', HERE / 'corpus.py']
    head = run(['git', '-C', str(project), 'rev-parse', 'HEAD'])
    dirty = run(['git', '-C', str(project), 'status', '--porcelain'])
    version = run([str(args.circt_opt), '--version'])
    summaries = Counter()
    records = []
    with tempfile.TemporaryDirectory(prefix='spatial-corpus-') as tmp:
        frontend_file = Path(tmp) / 'frontend.mlir'
        control_file = Path(tmp) / 'control.mem'
        for file in files:
            record = {'program': file.name, 'frontend': 'not_run', 'dialect': 'not_run',
                      'control_components': [],
                      'whole_program': {'status': 'not_run'}}
            front = run([str(args.frontend), 'frontend-mlir', str(file)])
            if front.returncode:
                record['frontend'] = 'failed'; record['error'] = last_error(front)
                records.append(record); continue
            record['frontend'] = 'exported'
            frontend_file.write_text(front.stdout)
            verify = run([str(args.circt_opt), f'--load-dialect-plugin={args.dialect}', str(frontend_file), '-o', '/dev/null'])
            if verify.returncode:
                record['dialect'] = 'failed'; record['error'] = last_error(verify)
                records.append(record); continue
            record['dialect'] = 'verified'
            model = run([str(args.frontend), 'model-ir', str(file)])
            if model.returncode:
                record['error'] = last_error(model)
                records.append(record); continue
            logical = json.loads(model.stdout)
            timers = [x['name'] for x in logical['items'] if x['kind'] == 'resource' and x['resource_kind'] == 'timer']
            shifters = [x['name'] for x in logical['items'] if x['kind'] == 'resource' and x['resource_kind'] == 'shifter']
            record['spi_component_probes'] = {'attempts': 0, 'matches': [], 'scope':
                'Timer/shifter component only; pin binding, control, streams and whole program are not mapped.'}
            for timer in timers:
                for shifter in shifters:
                    record['spi_component_probes']['attempts'] += 1
                    probe = run([str(args.mapper), '--derive-spi-component', str(frontend_file), timer, shifter, '0'])
                    if not probe.returncode:
                        match = re.search(r'width = (\d+) : i64, half_period = (\d+) : i64', probe.stdout)
                        if not match:
                            raise RuntimeError(f'{file.name}: malformed derived SPI component')
                        record['spi_component_probes']['matches'].append({
                            'timer': timer, 'shifter': shifter,
                            'width': int(match.group(1)), 'half_period_cycles': int(match.group(2))})
            full_file = Path(tmp) / 'full.mem'
            full_file.unlink(missing_ok=True)
            full = run([str(args.mapper), '--compile-control', str(frontend_file), str(full_file)])
            if full.returncode:
                if full_file.exists():
                    raise RuntimeError(f'{file.name}: rejected program produced an image')
                record['whole_program'] = {'status': 'unsupported', 'reason': last_error(full)}
            else:
                mapping = json.loads(full.stdout)
                proto = next(p for p in logical['protocols'] if p['name'] == mapping['protocol'])
                rows = [int(line, 16) for line in full_file.read_text().splitlines()]
                if len(rows) != 17 or not check_control(proto, rows[:16], mapping['state_map']):
                    raise RuntimeError(f'{file.name}: complete control image disagrees with source')
                record['whole_program'] = {'status': 'mapped', 'target': 'control-fabric-v1',
                                           'pin_bindings': mapping['pin_bindings']}
            for proto in logical['protocols']:
                component = {'name': proto['name'], 'states': proto['states'],
                             'inputs': len(proto['inputs']), 'source_input_binding': 'unverified'}
                if control_file.exists():
                    control_file.unlink()
                mapped = run([str(args.mapper), '--control-rows', str(frontend_file), proto['name'], str(control_file)])
                if mapped.returncode:
                    component['status'] = 'unsupported'
                    component['reason'] = last_error(mapped)
                else:
                    rows = [int(line, 16) for line in control_file.read_text().splitlines()]
                    mapping = json.loads(mapped.stdout)
                    if not check_control(proto, rows, mapping['state_map']):
                        raise RuntimeError(f'{file.name}/{proto["name"]}: control table disagrees with source semantics')
                    component['status'] = 'table_emitted_and_checked'
                    component['mapped_states'] = mapping['mapped_states']
                    component['reachable_states'] = mapping['reachable_states']
                    component['limitations'] = 'Input and output binding, placement, and surrounding program not mapped.'
                record['control_components'].append(component)
            record['unmodeled_word_streams'] = len(logical['connections']['streams'])
            records.append(record)
    for rec in records:
        summaries['total_programs'] += 1
        summaries['frontend_exported'] += rec['frontend'] == 'exported'
        summaries['dialect_verified'] += rec['dialect'] == 'verified'
        summaries['full_programs_mapped'] += rec['whole_program']['status'] == 'mapped'
        summaries['programs_with_explicit_protocol_graph'] += bool(rec['control_components'])
        probes = rec.get('spi_component_probes', {})
        summaries['spi_component_pairs_probed'] += probes.get('attempts', 0)
        summaries['spi_component_pairs_derived'] += len(probes.get('matches', []))
        for component in rec['control_components']:
            summaries['control_' + component['status']] += 1
    # Materialize zeros so the absence of support is visible in the report.
    for key in ('total_programs', 'frontend_exported', 'dialect_verified',
                'full_programs_mapped', 'programs_with_explicit_protocol_graph',
                'control_table_emitted_and_checked', 'control_unsupported'):
        summaries[key] += 0
    report = {'schema': 'spatial.circt-corpus-coverage.v1',
              'scope': 'Frontend, isolated control tables and restricted control-only target coverage; not general topology fit or physical timing.',
              'candidate': 'prototype-control-v1: 4 states, 2 inputs, 3 action outputs; reachable-state Mealy minimization',
              'provenance': {'frontend_git_head': head.stdout.strip() if not head.returncode else None,
                             'frontend_worktree_clean': not dirty.returncode and not dirty.stdout.strip(),
                             'rust_source_sha256': source_digest(rust_sources, project),
                             'mapper_source_sha256': source_digest(mapper_sources, project),
                             'corpus_sha256': source_digest(files, args.examples),
                             'circt_opt_version': version.stdout.strip() if not version.returncode else None},
              'summary': dict(sorted(summaries.items())), 'programs': records}
    text = json.dumps(report, indent=2, sort_keys=True) + '\n'
    if args.output:
        args.output.write_text(text)
    else:
        print(text, end='')
    if any(r['frontend'] != 'exported' or r['dialect'] != 'verified' or 'error' in r for r in records):
        return 1
    if args.require_full and summaries['full_programs_mapped'] != summaries['total_programs']:
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
