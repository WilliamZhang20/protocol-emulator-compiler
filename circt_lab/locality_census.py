"""Functional-locality census (ACTOR_FABRIC.md section 4, A4/A5 evidence).

For every complete protocol graph compiled on a contract, list each
producer -> consumer pair the mesh carries:
  - the producer and consumer kinds and ports,
  - the placed cells, their Manhattan distance and the routed hops,
  - the pair's event rate: `bit` (can change every bit or every clock:
    timer ticks, pin events, shifter bit outputs, CRC data, anything
    computed from them), `word` (once per word or frame), `txn` (once per
    transaction: machine and flag levels set by the host), `const`,
  - whether it is timing-critical: a bit-rate signal into a pad driver, a
    shifter's shift/sample/in, a CRC's absorb/data, or a timer/counter
    step (a missed or late edge is a protocol error, not a delay).
Then aggregates per kind pair over all programs: how often it occurs, how
far it travels, and how much of it is critical. That is the evidence for
tile arrangement and for optional direct links (a pair that is frequent,
bit-rate, critical and routed over several hops is a link candidate; a
linked resource stays usable through the mesh).

Rates are classes from the graph (the source kind and port and what it is
computed from), not measured toggles; a timer's period is exact.
"""
import json
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from fabric_build import build  # noqa: E402

RANK = {'const': 0, 'txn': 1, 'word': 2, 'bit': 3}
BIT_PORTS = {'shifter': {'out'}, 'crc': {'out'}}    # the rest of a shifter's/CRC's outputs: per word or frame
LOCALS = {'shifter': ['word', 'stop', 'out', 'active'], 'crc': ['ok', 'out', 'match', 'mismatch'],
          'timer': ['tick', 'level', 'rise', 'fall'], 'counter': ['at_least', 'reached'], 'logic': ['q0', 'q1', 'q2', 'q3']}


def port_name(kind, local):
    names = LOCALS.get(kind)
    if isinstance(local, int) and names and local < len(names):
        return names[local]
    return local if local not in (None, '') else (names[0] if names else '')
CRITICAL_SINKS = {('io_drv', 'value'), ('io_drv', 'enable'), ('shifter', 'shift'), ('shifter', 'in'),
                  ('shifter', 'sample'), ('crc', 'absorb'), ('crc', 'data'), ('crc', 'emit'),
                  ('timer', 'run'), ('timer', 'start'), ('counter', 'step'), ('counter', 'count')}


def source_rate(nl, sig, seen=()):
    """The rate class of a netlist signal and the timer period if a timer drives it."""
    s = nl.signals.get(sig)
    if s is None or s['type'] == 'const':
        return 'const', None
    if s['type'] != 'base':                       # a machine-selected signal: the max over its cases
        best = ('txn', None)
        for v in json.loads(json.dumps(s)).get('cases', {}).values():
            r = source_rate(nl, v, seen) if isinstance(v, str) else ('const', None)
            if RANK[r[0]] > RANK[best[0]]:
                best = r
        return best
    b = nl.blocks.get(s['block'])
    if b is None:                                 # the word port, a host flag
        return 'txn', None
    k = b['kind']
    local = port_name(k, s.get('local'))
    if k == 'timer':
        return 'bit', int(b.get('period', 0)) or None
    if k in ('io', 'io_in', 'io_edge') or k.startswith('io'):
        return 'bit', None
    if k in BIT_PORTS:
        return ('bit', None) if local in BIT_PORTS[k] else ('word', None)
    if k == 'counter':
        return 'word', None
    if k == 'logic':
        if s['block'] in seen:
            return 'txn', None
        best = ('txn', None)
        for c in nl.consumers:
            if c['block'] == s['block']:
                r = source_rate(nl, c['sig'], seen + (s['block'],))
                if RANK[r[0]] > RANK[best[0]]:
                    best = r
        return best
    return 'txn', None


def hops_to(tree, cell, slot):
    """Track hops on the routed path to ('ipin', cell, slot)."""
    parent = {b: a for a, b in tree}
    node, hops = ('ipin', cell, slot), 0
    while node in parent:
        node = parent[node]
        hops += node[0] == 'track'
    return hops


def census(contract, source):
    r = build(contract, source)[3]
    nl, binding, trees, cols = r['netlist'], r['binding'], r['routed']['trees'], contract['cols']
    rows = []
    sinks_of = {}
    for c in nl.consumers:
        sinks_of.setdefault(c['block'], []).append(c)
    for c in nl.consumers:
        sig = c['sig']
        s = nl.signals.get(sig, {'type': 'const'})
        if s['type'] == 'const':
            continue
        dst = c['block']
        if dst not in nl.blocks:
            continue
        dk = nl.blocks[dst]['kind']
        src = s['block'] if s['type'] == 'base' else sig
        sk = nl.blocks[src]['kind'] if src in nl.blocks else 'word_port' if src.split('/')[-1].startswith('#') else 'machine'
        sp = port_name(sk, s.get('local'))
        cs, cd = binding.get(src), binding.get(dst)
        dist = abs(cs // cols - cd // cols) + abs(cs % cols - cd % cols) if cs is not None and cd is not None else None
        hops = 0 if s['type'] != 'base' and sig not in trees else None   # an FSM's own sink: in its switch
        if sig in trees and cd is not None:
            ipins = [b for e in trees[sig] for b in e if b[0] == 'ipin' and b[1] == cd]
            hops = max((hops_to(trees[sig], b[1], b[2]) for b in ipins), default=None)
        rate, period = source_rate(nl, sig)
        rows.append({'src': src.split('/', 1)[-1], 'src_kind': sk, 'port': sp, 'dst': dst.split('/', 1)[-1],
                     'dst_kind': dk, 'sink': c['sink'], 'cells': [cs, cd], 'distance': dist, 'hops': hops,
                     'same_cell': cs == cd, 'rate': rate, 'period': period,
                     'critical': rate == 'bit' and (dk, c['sink']) in CRITICAL_SINKS})
    return {'program': Path(source).stem, 'pairs': rows, 'tracks_used': r['tracks_used'],
            'wirelength': r['wirelength'], 'max_hops': r['max_hops']}


def aggregate(results):
    agg = defaultdict(lambda: {'programs': set(), 'pairs': 0, 'bit': 0, 'critical': 0, 'hops': [], 'same_cell': 0})
    for res in results:
        for p in res['pairs']:
            a = agg[(p['src_kind'], p['dst_kind'])]
            a['programs'].add(res['program'])
            a['pairs'] += 1
            a['bit'] += p['rate'] == 'bit'
            a['critical'] += p['critical']
            a['same_cell'] += p['same_cell']
            if p['hops'] is not None:
                a['hops'].append(p['hops'])
    out = []
    for (sk, dk), a in sorted(agg.items(), key=lambda x: -x[1]['pairs']):
        h = a['hops']
        out.append({'pair': f'{sk} -> {dk}', 'programs': len(a['programs']), 'pairs': a['pairs'], 'bit': a['bit'],
                    'critical': a['critical'], 'same_cell': a['same_cell'],
                    'mean_hops': round(sum(h) / len(h), 2) if h else None, 'max_hops': max(h) if h else None,
                    'hop_total': sum(h)})
    return out


if __name__ == '__main__':
    import argparse
    from concurrent.futures import ProcessPoolExecutor
    from fabric_arch import logic_meshes
    from test_fabric_lower import EXAMPLES, MUST_COMPILE_LOGIC
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('--contract', default='mesh_3x5_L3S', help='a logic_meshes() name or a contract .json')
    ap.add_argument('--json', type=Path, help='write every pair and the aggregate here')
    a = ap.parse_args()
    con = next((c for c in logic_meshes() if c['name'] == a.contract), None) or json.loads(Path(a.contract).read_text())

    def src(prog):
        return next(p for p in (EXAMPLES / f'{prog}.spt', EXAMPLES / 'fabric' / f'{prog}.spt') if p.exists())

    def one(prog):
        try:
            return census(con, src(prog))
        except Exception as e:  # a program that does not map is reported, not counted
            return {'program': prog, 'error': f'{type(e).__name__}: {e}'[:200], 'pairs': []}
    with ProcessPoolExecutor(19) as ex:
        results = list(ex.map(one, sorted(MUST_COMPILE_LOGIC)))
    for res in results:
        if 'error' in res:
            print(f"{res['program']:38s} not mapped: {res['error']}")
            continue
        crit = [p for p in res['pairs'] if p['critical']]
        far = [p for p in crit if (p['hops'] or 0) >= 3]
        print(f"{res['program']:38s} pairs {len(res['pairs']):3d}  bit {sum(p['rate'] == 'bit' for p in res['pairs']):3d}"
              f"  critical {len(crit):3d}  critical >=3 hops {len(far):2d}  tracks {res['tracks_used']:3d}  wl {res['wirelength']}")
    print()
    print(f"{'pair':28s} {'progs':>5s} {'pairs':>5s} {'bit':>4s} {'crit':>4s} {'local':>5s} {'mean h':>6s} {'max h':>5s} {'sum h':>5s}")
    agg = aggregate(results)
    for g in agg:
        print(f"{g['pair']:28s} {g['programs']:5d} {g['pairs']:5d} {g['bit']:4d} {g['critical']:4d} {g['same_cell']:5d} "
              f"{str(g['mean_hops']):>6s} {str(g['max_hops']):>5s} {g['hop_total']:5d}")
    if a.json:
        a.json.write_text(json.dumps({'contract': con['name'], 'programs': results, 'aggregate': agg}, indent=1, default=list))
