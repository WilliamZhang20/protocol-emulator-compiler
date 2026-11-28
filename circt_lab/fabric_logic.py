#!/usr/bin/env python3
"""Logic for the mesh's logic tiles (mesh.v mesh_logic): truth tables from
source expressions, and packing LUTs into tiles before placement (the pack
stage of a pack -> place -> route flow, as VPR's).

A LUT is one register of the source (`reg r [on E] = f`) or one piece of
combinational source logic that no tile computes (a pin drive selected by a
flag, a connection that is a formula). Every LUT output is a register, so:
  - a `reg` maps exactly (q loads f, on E when it has a clock enable, and
    holds its start value while the fabric is stopped);
  - combinational source logic is one clock late (`lag` 1, reported; a
    paired pin drive gets the same lag on both pins).

Leaves (a LUT's inputs) are keys:
  'lut:NAME'   another LUT's register (inside the tile: its q; elsewhere: a
               routed signal);
  'flag:NAME'  a host flag (inside the tile: one of its four h bits);
  '@mux:JSON'  a selection by FSM state (a routed signal);
  anything else: a routed fabric signal ('timer.tick', a pin event...).

A flag that starts at 1 is stored inverted (h is 0 while the fabric is
stopped), so its h bit is the flag XOR its start value.
"""
import json


class LogicError(ValueError):
    pass


def evaluate(e, value, leaf_key):
    """Evaluate a source expression with `value(key)` for each leaf."""
    k = e['kind']
    if k == 'integer':
        v = int(e['value'], 0)
        if v not in (0, 1):
            raise LogicError(f'constant {v} on a 1-bit signal')
        return v
    if k == 'unary' and e['op'] == '!':
        return 1 - evaluate(e['value'], value, leaf_key)
    if k == 'binary':
        a, b = evaluate(e['left'], value, leaf_key), evaluate(e['right'], value, leaf_key)
        ops = {'&&': a & b, '||': a | b, '&': a & b, '|': a | b, '^': a ^ b, '!=': a ^ b, '==': 1 - (a ^ b)}
        if e['op'] not in ops:
            raise LogicError(f'operator {e["op"]} on 1-bit signals')
        return ops[e['op']]
    if k == 'if':
        c = evaluate(e['condition'], value, leaf_key)
        return evaluate(e['yes'] if c else e['no'], value, leaf_key)
    key = leaf_key(e)
    if isinstance(key, int):
        return key
    return value(key)


def leaves_of(e, leaf_key, out):
    """The distinct leaf keys of an expression, in first-use order."""
    k = e['kind']
    if k == 'integer':
        return out
    if k == 'unary':
        return leaves_of(e['value'], leaf_key, out)
    if k == 'binary':
        leaves_of(e['left'], leaf_key, out)
        return leaves_of(e['right'], leaf_key, out)
    if k == 'if':
        for part in ('condition', 'yes', 'no'):
            leaves_of(e[part], leaf_key, out)
        return out
    key = leaf_key(e)
    if not isinstance(key, int) and key not in out:
        out.append(key)
    return out


def make_lut(name, expr, leaf_key, enable=None, init=0, flag_init=None, lag=0, inputs=4):
    """A LUT {name, leaves, truth, enable, init, lag}. `enable` is a leaf
    key (a routed signal: the tile's enable select reads a sink) or None.
    An enable that is not a routed signal (a flag, a LUT register) is folded
    into the function: q loads (E ? f : q)."""
    flag_init = flag_init or {}
    leaves = leaves_of(expr, leaf_key, [])
    if enable is not None and (enable.startswith('lut:') or enable.startswith('flag:')):
        en_key, enable = enable, None
        if en_key not in leaves:
            leaves.append(en_key)
        me = f'lut:{name}'
        if me not in leaves:
            leaves.append(me)
    else:
        en_key = None
    if len(leaves) > inputs:
        raise LogicError(f'{len(leaves)} inputs ({", ".join(leaves)}); a LUT has {inputs}')
    truth = 0
    for idx in range(1 << len(leaves)):
        bits = {k: (idx >> i) & 1 for i, k in enumerate(leaves)}

        def value(k):
            v = bits[k]
            if k.startswith('flag:'):            # the h bit is stored XOR the flag's start value
                v ^= flag_init.get(k[5:], 0)
            return v
        f = evaluate(expr, value, leaf_key)
        if en_key is not None and not value(en_key):
            f = bits[f'lut:{name}']
        # replicate over the unused high inputs (they select constant 0 anyway)
        truth |= f << idx
    return {'name': name, 'leaves': leaves, 'truth': truth, 'enable': enable, 'init': int(init), 'lag': lag}


def external(lut, members):
    """Leaf keys a LUT reads through the tile's sinks, if it sits in a tile
    holding `members` (LUT names)."""
    out = [k for k in lut['leaves'] if not k.startswith('flag:') and not (k.startswith('lut:') and k[4:] in members)]
    if lut['enable'] is not None:
        out.append(lut['enable'])
    return out


def fits(luts, kind, read_outside):
    """Can these LUTs share a tile: LUTs, external inputs (sinks), flags and
    outputs (registers read outside the tile: only q0-q3 leave it)."""
    members = {l['name'] for l in luts}
    ext = {k for l in luts for k in external(l, members)}
    flags = {k for l in luts for k in l['leaves'] if k.startswith('flag:')}
    outs = [l for l in luts if read_outside(l['name'], members)]
    return len(luts) <= kind['luts'] and len(ext) <= kind.get('lut_sinks', len(kind['sinks'])) and len(flags) <= kind['flags'] and \
        len(outs) <= kind.get('outputs', kind['luts'])


def pack_once(luts, kind, exported, rng):
    """One greedy clustering: seed with a LUT (the most inputs first, ties
    random), then add the LUT sharing the most inputs and connections with
    the cluster while it fits. A LUT read by `exported` users (a route, a
    host wait) or by a LUT outside the cluster is an output."""
    readers = {}
    for l in luts.values():
        for k in l['leaves'] + ([l['enable']] if l['enable'] else []):
            if k.startswith('lut:'):
                readers.setdefault(k[4:], set()).add(l['name'])

    def read_outside(name, members):
        return name in exported or bool(readers.get(name, set()) - members)
    left = sorted(luts.values(), key=lambda l: (-len(l['leaves']), rng.random()))
    tiles = []
    while left:
        cl = [left.pop(0)]
        while True:
            best, score = None, -1
            members = {l['name'] for l in cl}
            ext = {k for l in cl for k in external(l, members)}
            flags = {k for l in cl for k in l['leaves'] if k.startswith('flag:')}
            for cand in left:
                if not fits(cl + [cand], kind, read_outside):
                    continue
                keys = set(cand['leaves']) | ({cand['enable']} if cand['enable'] else set())
                s = 2 * len(keys & ext) + len(keys & flags) + \
                    3 * sum(1 for k in keys if k.startswith('lut:') and k[4:] in members) + \
                    3 * sum(1 for l in cl if f'lut:{cand["name"]}' in l['leaves']) + rng.random()
                if s > score:
                    best, score = cand, s
            if best is None:
                break
            cl.append(best)
            left.remove(best)
        members = {l['name'] for l in cl}
        cl.sort(key=lambda l: not read_outside(l['name'], members))      # only q0-q3 leave the tile
        tiles.append(cl)
    cut = sum(1 for cl in tiles for l in cl if read_outside(l['name'], {m['name'] for m in cl}))
    return tiles, cut


def pack(luts, kind, exported=(), tries=300):
    """The best of several greedy clusterings: fewest tiles, then fewest
    outputs (signals routed between tiles). Deterministic (seeded)."""
    import random
    best = None
    for seed in range(tries):
        tiles, cut = pack_once(luts, kind, exported, random.Random(seed))
        key = (len(tiles), cut)
        if best is None or key < best[0]:
            best = (key, tiles)
    return best[1]


def tiles_of(luts, kind, flag_init, exported=(), prefix='_logic'):
    """Pack and encode. Returns (tiles {name: tile spec}, connect {tile.sK:
    key}, alias {'lut:NAME': 'tile.qJ'}, flag_slots {flag: [(tile, slot)]})."""
    sel = kind['select']
    tiles, connect, alias, slots = {}, {}, {}, {}
    for i, cl in enumerate(pack(luts, kind, exported)):
        t = f'{prefix}{i}'
        members = {l['name']: j for j, l in enumerate(cl)}
        sinks, flags = [], []
        for l in cl:
            for k in external(l, members):
                if k not in sinks:
                    sinks.append(k)
            for k in l['leaves']:
                if k.startswith('flag:') and k not in flags:
                    flags.append(k)
        spec = []
        for j, l in enumerate(cl):
            alias[f'lut:{l["name"]}'] = f'{t}.q{j}'
            select = []
            for k in l['leaves']:
                if k.startswith('flag:'):
                    select.append(sel['h_first'] + flags.index(k))
                elif k.startswith('lut:') and k[4:] in members:
                    select.append(sel['q_first'] + members[k[4:]])
                else:
                    select.append(sel['sink_first'] + sinks.index(k))
            select += [sel['zero']] * (4 - len(select))
            en = kind['enable']['always'] if l['enable'] is None else kind['enable']['sink_first'] + sinks.index(l['enable'])
            spec.append({'name': l['name'], 'truth': l['truth'], 'select': select, 'enable': en, 'init': l['init']})
        tiles[t] = {'kind': 'logic', 'luts': spec, 'flags': [f[5:] for f in flags]}
        for s, k in enumerate(sinks):
            connect[f'{t}.s{s}'] = k
        for s, f in enumerate(flags):
            slots.setdefault(f[5:], []).append((t, s))
    return tiles, connect, alias, slots


def flag_writes(slots, flag_init, values):
    """Parameter writes for the flags in `values` {flag: 0/1}: one per tile
    holding any of them, data[3:0] the h bits (flag XOR start value) and
    data[7:4] the mask of the flags written (the tile's others keep theirs)."""
    per = {}
    for f, where in slots.items():
        if f not in values:
            continue
        for t, s in where:
            v, m = per.get(t, (0, 0))
            per[t] = (v | ((values[f] ^ flag_init.get(f, 0)) << s), m | (1 << s))
    return [{'op': 'param', 'tile': t, 'reg': 'flags', 'value': v | (m << 4)} for t, (v, m) in sorted(per.items())]


def mux_key(d):
    return '@mux:' + json.dumps(d, sort_keys=True)


def unkey(k):
    return json.loads(k[5:]) if isinstance(k, str) and k.startswith('@mux:') else k
