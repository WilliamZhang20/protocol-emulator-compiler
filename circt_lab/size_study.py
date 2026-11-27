#!/usr/bin/env python3
"""Grid size, resource mix and context study (FABRIC_REORG.md steps 6R/8):
compile every example against candidate contracts and report, per
candidate, which programs map and why the others do not.

  python3 size_study.py [--jobs 8] [--only NAME,...]

A candidate is rows x columns, a kind per cell (row-major) and a context
count per cell. Failures keep their class: 'hardware' and 'resources' /
'rules' are limits of the candidate, 'compiler' is not yet lowered on any
candidate, 'timing' and 'route' come from the heuristic mapper (400
placements), so a heuristic failure is not a proof that it cannot map.
"""
import argparse
import json
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

from fabric_arch import make_contract
from fabric_build import build
from fabric_lower import LowerError
from fabric_map import MapError
from test_fabric_lower import EXAMPLES

T, I, S, C, K, L = 'timer', 'io', 'shifter', 'crc', 'counter', 'logic'


def candidate(name, rows, cols, kinds, ctx4_kinds, word_port):
    ctx = [4 if k in ctx4_kinds else 1 for k in kinds]
    return make_contract(rows, cols, kinds, ctx, word_port, name)


CANDIDATES = [
    # The chip today (test_3x5c): contexts on io, shifter and crc cells.
    candidate('3x5_ctx4', 3, 5, [T, I, T, I, T, S, C, S, C, S, T, I, K, I, T], (I, S, C), 6),
    candidate('3x5_ctx1', 3, 5, [T, I, T, I, T, S, C, S, C, S, T, I, K, I, T], (), 6),
    # 3 x 4: one timer and one shifter fewer.
    candidate('3x4_ctx4', 3, 4, [T, I, I, T, S, C, S, C, T, I, K, I], (I, S, C), 5),
    candidate('3x4_ctx1', 3, 4, [T, I, I, T, S, C, S, C, T, I, K, I], (), 5),
    # 2 x 5: io beside the crc and shifter row.
    candidate('2x5_ctx4', 2, 5, [T, I, S, I, T, S, C, K, C, S], (I, S, C), 6),
    candidate('2x5_ctx1', 2, 5, [T, I, S, I, T, S, C, K, C, S], (), 6),
]

# Fewer contexted cells: the mesh's state machines live in a cell's
# contexts, so contexts are its FSM resource, not a multi-protocol feature.
def partial(name, rows, cols, kinds, ctx_cells, word_port):
    return make_contract(rows, cols, kinds, [4 if i in ctx_cells else 1 for i in range(rows * cols)],
                         word_port, name)


K34 = [T, I, I, T, S, C, S, C, T, I, K, I]
PARTIAL = [
    partial('3x4_ctxS', 3, 4, K34, {4, 6}, 5),            # the two shifters
    partial('3x4_ctxSC', 3, 4, K34, {4, 5, 6, 7}, 5),     # shifters and crcs (middle row)
    partial('3x4_ctxIO', 3, 4, K34, {1, 2, 9, 11}, 5),    # io cells
    partial('3x4_ctxSIO', 3, 4, K34, {1, 2, 4, 6, 9, 11}, 5),
]


# Logic cells (mesh_logic: eight LUT4s with registers, four host flags) in
# place of the less used coarse cells. Contexts on io, shifter and crc cells.
def logic(name, rows, cols, kinds, word_port):
    return make_contract(rows, cols, kinds, [4 if k in (I, S, C) else 1 for k in kinds], word_port, name)


LOGIC = [
    # 3 x 5: shifters both sides of the word-port crc (the I2C programs'
    # machines route only so), io top centre and right, logic beside them.
    logic('3x5_L3', 3, 5, [T, L, I, L, T, S, C, S, C, I, T, K, L, K, T], 6),
]


def one(args):
    cand, src = args
    try:
        r = build(cand, Path(src))[3]
        return cand['name'], Path(src).stem, 'compiled', f'{r["tracks_used"]} tracks, {r["max_hops"]} hops'
    except LowerError as e:
        return cand['name'], Path(src).stem, e.cls, str(e).split(': ', 1)[-1][:160]
    except MapError as e:
        return cand['name'], Path(src).stem, f'map {e.cls}', str(e)[:160]
    except Exception as e:                              # noqa: BLE001 (a crash is a result too)
        return cand['name'], Path(src).stem, 'crash', f'{type(e).__name__}: {e}'[:160]


def main():
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('--jobs', type=int, default=8)
    ap.add_argument('--only', help='comma-separated program names')
    ap.add_argument('--json', type=Path)
    ap.add_argument('--partial', action='store_true', help='the partial-context candidates')
    ap.add_argument('--logic', action='store_true', help='the candidates with logic cells')
    a = ap.parse_args()
    srcs = sorted(EXAMPLES.glob('*.spt')) + sorted(EXAMPLES.glob('fabric/*.spt'))
    if a.only:
        keep = set(a.only.split(','))
        srcs = [s for s in srcs if s.stem in keep]
    cands = PARTIAL if a.partial else LOGIC if a.logic else CANDIDATES
    work = [(c, str(s)) for c in cands for s in srcs]
    with ProcessPoolExecutor(a.jobs) as ex:
        results = list(ex.map(one, work))
    table = {}
    for cand, prog, cls, msg in results:
        table.setdefault(prog, {})[cand] = (cls, msg)
    names = [c['name'] for c in cands]
    print(f'{"program":36s} ' + ' '.join(f'{n:>10s}' for n in names))
    for prog in sorted(table):
        row = table[prog]
        if all(row[n][0] == 'compiler' for n in names):
            continue                                    # not lowered on any candidate: says nothing about size
        print(f'{prog:36s} ' + ' '.join(f'{("ok" if row[n][0] == "compiled" else row[n][0]):>10s}' for n in names))
    print()
    for n in names:
        ok = sum(1 for p in table if table[p][n][0] == 'compiled')
        print(f'{n}: {ok} compiled')
    print('\nwhy (first candidate where each program fails):')
    for prog in sorted(table):
        for n in names:
            cls, msg = table[prog][n]
            if cls not in ('compiled', 'compiler'):
                print(f'  {prog} on {n}: {cls}: {msg}')
    if a.json:
        a.json.write_text(json.dumps(table, indent=1) + '\n')


if __name__ == '__main__':
    main()
