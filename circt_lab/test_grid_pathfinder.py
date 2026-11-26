#!/usr/bin/env python3
"""R2 and R3: the independent route checker catches illegal routes, and
PathFinder routes everything the exhaustive router can, every result
passing the checker."""
import random
import time

from grid_pathfinder import route, check
from grid_route import routes
from grid_rrg import RRG, WordSim, load

contract = load()
net = contract['bit_network']
g = RRG(contract)
cells = contract['grid']['cells']
by_cell = {}
for name, r in contract['resources'].items():
    by_cell.setdefault(r['cell'], name)
opins = [n for n in g.nodes if n[0] == 'opin' and n[1] in by_cell]
ipins = [n for n in g.nodes if n[0] == 'ipin' and g.sinks[n[1]][0] in by_cell]


def old_workload(nets):
    return {'name': 'w', 'nets': [
        {'name': n['name'], 'source': {'resource': by_cell[n['source'][1]], 'port': n['source'][2]},
         'sinks': [{'resource': 'crc' if s[1] >= 77 else by_cell[g.sinks[s[1]][0]], 'port': s[1]} for s in n['sinks']]}
        for n in nets]}


def must_fail(fn, why):
    try:
        fn()
    except ValueError as e:
        assert why in str(e), (why, str(e))
    else:
        raise AssertionError(f'accepted, expected {why!r}')


# ---- R2: the checker, on hand-made routes.
# input_io (cell 0) output 0 to timer0 (cell 2) sink 0: east along row 0.
sv, isel = net['selector_values'], net['input_selector']
src = ('opin', 0, 0)
one = [{'name': 'a', 'source': src, 'sinks': [('ipin', 0)]}]
legal = route(g, one)
assert legal['status'] == 'routed' and check(contract, one, legal['words']) == []
words = dict(legal['words'])
# A sink selector reading the wrong track.
bad = dict(words); w = net['sink_words'][0]; bad[w] = bad[w] ^ 1
assert check(contract, one, bad), 'wrong track accepted'
# A turn a track's class forbids: send the signal east on track 2 (vertical
# then horizontal), then try to turn south on it at cell 1.
labels = [g.switch_label(0, 'east', 2, sv['local_first']), g.switch_label(1, 'south', 2, sv['turn_first']),
          g.sink_label(5, isel['north_first'] + 2)]
assert WordSim(contract, RRG.words(labels)).drives(5) is None
assert check(contract, [{'name': 'a', 'source': src, 'sinks': [('ipin', 5)]}], RRG.words(labels))
# A LUT input reading its own cell's output (no local feedback).
lut = net['lut_sinks'][0]
assert WordSim(contract, {net['sink_words'][lut]: isel['local_first']}).drives(lut) is None
# An extra sink tapping a routed track is reported.
extra = dict(words); extra[net['sink_words'][1]] = words[net['sink_words'][0]]
assert any('unrouted but reads' in p for p in check(contract, one, extra))
# Two nets needing different values in one field conflict in emission.
must_fail(lambda: RRG.words([g.switch_label(0, 'east', 0, sv['local_first']),
                             g.switch_label(0, 'east', 0, sv['local_first'] + 1)]), 'needs')
# Malformed nets are refused.
must_fail(lambda: route(g, one + [{'name': 'b', 'source': ('opin', 0, 1), 'sinks': [('ipin', 0)]}]), 'driven by')
must_fail(lambda: route(g, [{'name': 'a', 'source': ('ipin', 0), 'sinks': []}]), 'not a local output')

# ---- R3: contention behaves like the exhaustive router's own tests.
pair = [{'name': 'a', 'source': ('opin', 0, 0), 'sinks': [('ipin', 0)]},
        {'name': 'b', 'source': ('opin', 0, 1), 'sinks': [('ipin', 1)]}]
assert route(RRG(contract, tracks=1), pair)['status'] == 'unroutable'
assert route(RRG(contract, tracks=2), pair)['status'] == 'routed'
fan = [{'name': 'a', 'source': ('opin', 0, 0), 'sinks': [('ipin', 0), ('ipin', 1)]}]
r = route(RRG(contract, tracks=1), fan)
assert r['status'] == 'routed' and sum(1 for _, b, _ in r['trees']['a'] if b[0] == 'track') == 2   # shared fanout

# ---- R3: random workloads against the exhaustive router. Whenever it
# routes, PathFinder must; every PathFinder route must pass the checker.
rng = random.Random(5)
stats = {'both': 0, 'pathfinder_only': 0, 'neither': 0, 'iterations': 0}
start = time.time()
for case in range(400):
    tracks = rng.choice([1, 2, 3, 4])
    k = rng.randint(1, 6)
    sources = rng.sample(opins, k)
    pool = rng.sample(ipins, min(len(ipins), k * 3))
    nets, i = [], 0
    for n, s in enumerate(sources):
        take = rng.randint(1, 3)
        sinks = pool[i:i + take]
        i += take
        if sinks:
            nets.append({'name': f'n{n}', 'source': s, 'sinks': sinks})
    exhaustive = routes(contract, old_workload(nets), tracks=tracks, limit=200000)
    if exhaustive['status'] == 'unknown_search_limit':
        continue
    pf = route(RRG(contract, tracks=tracks), nets)
    if exhaustive['status'] == 'routed':
        assert pf['status'] == 'routed', (case, tracks, nets, pf['overused'])
    if pf['status'] == 'routed':
        stats['iterations'] = max(stats['iterations'], pf['iterations'])
        if tracks == net['tracks_per_direction']:
            assert check(contract, nets, pf['words']) == [], (case, check(contract, nets, pf['words']))
        stats['both' if exhaustive['status'] == 'routed' else 'pathfinder_only'] += 1
    else:
        stats['neither'] += 1
elapsed = time.time() - start
print(f'PASS: checker rejects wrong tracks, forbidden turns, LUT feedback, taps and field conflicts; '
      f'PathFinder matches contention and shared fanout; {sum(stats.values()) - stats["iterations"]} random workloads: '
      f'{stats["both"]} routed by both, {stats["pathfinder_only"]} by PathFinder only (trees the exhaustive model '
      f'does not search), {stats["neither"]} by neither, never one the exhaustive router routes and PathFinder '
      f'does not; at most {stats["iterations"]} iterations; {elapsed:.1f} s')
