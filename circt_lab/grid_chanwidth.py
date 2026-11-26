#!/usr/bin/env python3
"""Minimum channel width over real protocol workloads (PLACE_ROUTE.md,
milestone R6): the co-design loop's measurement.

A routing workload (grid_corpus.py / grid_workload.py output) names fixed
resources (timer0, shifter1, ...). Here every resource becomes a block of
its kind, so placement may rebind it; several protocols run at once by
giving each its own blocks. For each workload or combination, the
smallest track count at which placement + PathFinder succeed is the
minimum channel width; VPR's practice adds ~30 % margin for the hardware.

    python3 grid_chanwidth.py WORKLOAD.json ... [--max-tracks 8] [--combine]
"""
import argparse
import itertools
import json
from pathlib import Path

from grid_place import instances, place_and_route
from grid_rrg import load


def netlist(contract, workload, prefix=''):
    """(blocks, nets) from a fixed-resource workload; ports become indices
    into the kind's port lists. Raises ValueError for a resource placement
    cannot move (a shared cell such as the CRC or memory)."""
    inst = instances(contract)
    kind_of = {name: k for k, rows in inst.items() for name, _, _, _ in rows}
    ports = {name: (sinks, outs) for k, rows in inst.items() for name, _, sinks, outs in rows}
    blocks, nets = {}, []
    for n in workload['nets']:
        src = n['source']['resource']
        sinks_list, outs = ports[src]
        if n['source']['port'] >= outs:
            raise ValueError(f'{workload["name"]}: {src} output {n["source"]["port"]} is not a placeable port')
        blocks[prefix + src] = kind_of[src]
        sinks = []
        for s in n['sinks']:
            r = s['resource']
            slist, _ = ports[r]
            if s['port'] not in slist:
                raise ValueError(f'{workload["name"]}: {r} input sink {s["port"]} is not a placeable port')
            blocks[prefix + r] = kind_of[r]
            sinks.append((prefix + r, slist.index(s['port'])))
        nets.append({'name': prefix + n['name'], 'source': (prefix + src, n['source']['port']), 'sinks': sinks})
    return blocks, nets


def fits(contract, blocks):
    inst = instances(contract)
    need = {}
    for k in blocks.values():
        need[k] = need.get(k, 0) + 1
    return all(need[k] <= len(inst[k]) for k in need)


def min_width(contract, blocks, nets, max_tracks=8, seed=1):
    """Smallest track count that places and routes, and the result there;
    also confirms one more track still routes (monotonicity)."""
    for w in range(1, max_tracks + 1):
        result = place_and_route(contract, blocks, nets, seed=seed, tracks=w)
        if result['status'] == 'routed':
            if w < max_tracks:
                assert place_and_route(contract, blocks, nets, seed=seed, tracks=w + 1)['status'] == 'routed', \
                    'routable at {w} tracks but not at {w + 1}'
            return w, result
    return None, None


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('workloads', nargs='+', type=Path)
    ap.add_argument('--max-tracks', type=int, default=8)
    ap.add_argument('--combine', action='store_true', help='also every combination that fits the instances')
    args = ap.parse_args()
    contract = load()
    native = contract['bit_network']['tracks_per_direction']
    loaded = []
    for p in args.workloads:
        w = json.loads(p.read_text())
        try:
            loaded.append((w['name'], *netlist(contract, w, prefix=w['name'] + '.')))
        except ValueError as e:
            print(f'{w["name"]}: skipped ({e})')
    rows = []
    groups = [[x] for x in loaded]
    if args.combine:
        for size in range(2, len(loaded) + 1):
            for combo in itertools.combinations(loaded, size):
                blocks = {k: v for _, b, _ in combo for k, v in b.items()}
                if fits(contract, blocks):
                    groups.append(list(combo))
    for group in groups:
        blocks = {k: v for _, b, _ in group for k, v in b.items()}
        nets = [n for _, _, ns in group for n in ns]
        w, result = min_width(contract, blocks, nets, args.max_tracks)
        rows.append({'workloads': [g[0] for g in group], 'blocks': len(blocks), 'nets': len(nets),
                     'sinks': sum(len(n['sinks']) for n in nets), 'min_tracks': w,
                     'wirelength': result['wirelength'] if result else None})
    print(json.dumps({'schema': 'spatial.channel-width.v1', 'contract_tracks': native, 'rows': rows}, indent=2))


if __name__ == '__main__':
    main()
