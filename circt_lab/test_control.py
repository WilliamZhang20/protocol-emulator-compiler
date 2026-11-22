#!/usr/bin/env python3
"""Differentially check source-to-control lowering against the Rust model."""
import argparse
import json
from pathlib import Path
import random
import subprocess
import tempfile
from corpus import check_control


def call(argv):
    return subprocess.run(argv, text=True, capture_output=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--frontend', required=True)
    parser.add_argument('--mapper', required=True)
    parser.add_argument('--cases', type=int, default=40)
    args = parser.parse_args()
    if args.cases < 1:
        parser.error('cases must be positive')
    rng = random.Random(0xC07A)
    counts = [0, 0]
    with tempfile.TemporaryDirectory(prefix='spatial-control-test-') as tmp:
        source, ir, image = (Path(tmp) / name for name in ('program.spt', 'program.mlir', 'control.mem'))
        for case in range(args.cases):
            states = rng.randint(1, 7)
            input_count = rng.randint(1, 2)
            text = 'clock 50 MHz\npin a: in\n'
            if input_count == 2:
                text += 'pin b: in\n'
            text += 'protocol machine on a' + (', b' if input_count == 2 else '') + ' {\n'
            emitted = 0
            for state in range(states):
                for mask in range(1 << input_count):
                    if rng.random() < 0.3 and not (state == 0 and mask == 0):
                        continue
                    bits = ' '.join('high' if mask & (1 << i) else 'low' for i in range(input_count))
                    dest = rng.randrange(states)
                    action = rng.randrange(4)
                    text += f'  transition {state} {bits} -> {dest}' + (f' action {action}' if action else '') + '\n'
                    emitted += 1
            text += '}\n'
            assert emitted
            source.write_text(text)
            front = call([args.frontend, 'frontend-mlir', str(source)])
            model = call([args.frontend, 'model-ir', str(source)])
            assessment = call([args.frontend, 'model-protocol', str(source), '--max-states', '4', '--max-inputs', '2', '--max-action', '3'])
            if front.returncode or model.returncode or assessment.returncode:
                raise AssertionError(f'case {case}: frontend failed: {front.stderr} {model.stderr} {assessment.stderr}\n{text}')
            ir.write_text(front.stdout)
            proto = json.loads(model.stdout)['protocols'][0]
            reference = json.loads(assessment.stdout)['results'][0]['assessment']
            if image.exists():
                image.unlink()
            mapped = call([args.mapper, '--control-rows', str(ir), 'machine', str(image)])
            expected = reference['capacity_fits']
            counts[expected] += 1
            if (mapped.returncode == 0) != expected:
                raise AssertionError(f'case {case}: expected fit={expected}, mapper: {mapped.stderr}\n{text}')
            if not expected:
                if image.exists():
                    raise AssertionError(f'case {case}: failed mapping wrote an image')
                continue
            report = json.loads(mapped.stdout)
            table = [int(line, 16) for line in image.read_text().splitlines()]
            if not check_control(proto, table, report['state_map']):
                raise AssertionError(f'case {case}: table differs from source semantics\n{text}')
            rust_classes = {frozenset(group) for group in reference['analysis']['equivalence_classes']}
            emitted_classes = {}
            for original, mapped_state in enumerate(report['state_map']):
                if mapped_state is not None:
                    emitted_classes.setdefault(mapped_state, set()).add(original)
            if rust_classes != {frozenset(group) for group in emitted_classes.values()}:
                raise AssertionError(f'case {case}: state minimization differs from Rust model\n{text}')
    if not all(counts):
        raise AssertionError(f'seeded sample needs both fits and rejections: {counts}')
    print(f'PASS: {args.cases} source control graphs agree with Rust reference ({counts[1]} fit, {counts[0]} reject)')


if __name__ == '__main__':
    main()
