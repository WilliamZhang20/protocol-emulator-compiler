#!/usr/bin/env python3
"""Display a typed graph mapper JSON report as a compact resource map."""
import argparse
from collections import Counter
import json
from pathlib import Path
import sys


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('report', nargs='?', type=Path, help='JSON report; default: stdin')
    args = parser.parse_args()
    try:
        report = json.loads(args.report.read_text() if args.report else sys.stdin.read())
        if report.get('model') != 'typed-resource-graph-v1' or 'resources' not in report:
            raise ValueError('expected a typed-resource graph report with resources')
        resources = report['resources']
        counts = Counter(r['kind'] for r in resources)
        used = Counter(r['kind'] for r in resources if r['assigned_nodes'])
        print('Model resource map (assignments; not hardware utilization)')
        print('  ' + '  '.join(f'{k}: {used[k]}/{counts[k]} tiles' for k in sorted(counts)))
        for r in resources:
            names = ', '.join(r['assigned_nodes']) if r['assigned_nodes'] else '(unused)'
            print(f"  [{r['tile']}] {r['kind']} i{r['width']}: {names}")
        print('\nModel routes')
        for route in report['routes']:
            print(f"  {route['from']} -> {route['to']} i{route['width']} "
                  f"({route['mode']}): {' -> '.join(route['tiles'])}")
        print(f"\nAbstract schedule: makespan {report['model_makespan']} cycles; "
              f"aggregate resource wait {report['resource_wait_cycles']} cycles")
        print('Not RTL timing or a configuration image. ' + report['limitations'])
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(f'cannot display graph report: {exc}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
