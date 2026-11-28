#!/usr/bin/env python3
"""Source -> fabric lowering (FABRIC_REORG.md step 4, L1-L2) and the
source-to-pins build (fabric_build.py).

- The tile register field maps (fabric_arch.REGISTERS) agree with the
  hardware's own (architecture/grid_5x3.json lane_core.registers) on every
  field the hardware publishes.
- Every example program either compiles (lowered, placed, routed and
  passed the independent checker) or is refused with a class; nothing
  fails any other way. The table is the current coverage of the examples.
- The programs the RTL repository runs at the mesh's pins through its SRAM
  (experiments/grid_fabric/reorg/compiled/*.plan.json, make
  reorg-compiled-test) are exactly what this compiler writes.
"""
import json
import os
import tempfile
from pathlib import Path

from fabric_arch import REGISTERS, logic_meshes, test_mesh
from fabric_build import FRONTEND, build, write
from fabric_lower import LowerError
from fabric_map import MapError

HERE = Path(__file__).resolve().parent
EXAMPLES = HERE.parent / 'examples'
# Programs that must compile today; the rest of the table may only grow.
MUST_COMPILE = {'i2c_target', 'i2c_uart_tx', 'jtag', 'loopback', 'memory_bandwidth', 'memory_bandwidth_bit2',
                'memory_bandwidth_fast', 'protocol_control', 'protocol_control_controller',
                'protocol_control_extended_actor', 'protocol_control_extended_actor_fast', 'ps2', 'spi',
                'stored_bytes', 'swd', 'uart'}
# With logic cells: everything above (no protocol is lost) and the programs
# that need logic (USB, Ethernet).
MUST_COMPILE_LOGIC = MUST_COMPILE | {'usb', 'ethernet_tx_coded', 'crc_link_trace',
                                     # the full-protocol workload (ACTOR_FABRIC.md section 10)
                                     'usb_device_rx', 'usb_device', 'usb_device_in', 'ethernet_rx', 'i2c_target_rw', 'spi_target', 'ps2_host'}
# Proven resource limits (the lowering's hardware refusals), by contract:
# the USB device's answers (OUT, IN) need a third timer (receive bit clock,
# turnaround, transmit bit clock) and 3 x 4 has two.
HARDWARE_REFUSED = {'mesh_3x4_L3': {'usb_device', 'usb_device_in'}}


def check_registers(rtl):
    hw = json.loads((rtl / 'architecture/grid_5x3.json').read_text())['lane_core']['registers']
    tiles = {'coder': 'shifter', 'crc': 'crc'}
    checked = 0
    for key, spec in hw.items():
        tile, reg = key.split('.')
        ours = REGISTERS[tiles[tile]].get(reg)
        if 'fields' not in spec:
            continue
        assert ours, f'{key}: the hardware publishes fields this compiler does not know'
        for field, bits in spec['fields'].items():
            assert ours['fields'].get(field) == bits, f'{key}.{field}: hardware {bits}, compiler {ours["fields"].get(field)}'
            checked += 1
        for enum in set(spec) - {'word', 'words', 'fields'}:
            assert ours.get(enum) == spec[enum], f'{key}.{enum}: encodings differ'
    print(f'register maps agree with the hardware on {checked} fields')


def coverage(contract, must, frontend, rtl):
    """Every program on one contract: those in `must` compile, and the RTL
    repository's committed programs for it (if any) match this compiler."""
    compiled, refused = set(), {'hardware': 0, 'compiler': 0}
    for src in sorted(EXAMPLES.glob('*.spt')) + sorted(EXAMPLES.glob('fabric/*.spt')):
        try:
            r = build(contract, src, frontend)[3]
            compiled.add(src.stem)
            result = f'compiled: {r["tracks_used"]} track segments, longest route {r["max_hops"]} hops, check passed'
        except LowerError as e:
            refused[e.cls] += 1
            result = f'refused ({e.cls}): {str(e).split(": ", 1)[-1]}'
        except MapError as e:
            refused.setdefault(f'map {e.cls}', 0)
            refused[f'map {e.cls}'] += 1
            result = f'map refused ({e.cls}): {e}'
        print(f'  {src.stem:38s} {result[:140]}')
    missing = must - compiled
    assert not missing, f'{contract["name"]}: no longer compiles: {sorted(missing)}'
    print(f'coverage on {contract["name"]}: {len(compiled)} compiled, '
          + ', '.join(f'{n} refused ({k})' for k, n in refused.items()))

    committed = rtl / 'experiments/grid_fabric/reorg/compiled' if rtl else None
    plans = sorted(committed.glob(f'*.{contract["name"]}.plan.json')) if committed and committed.exists() else []
    if plans:
        assert {p.name.split('.')[0] for p in plans} == must, \
            f'RTL repository runs {[p.name for p in plans]}, expected {sorted(must)}'
        with tempfile.TemporaryDirectory() as tmp:
            for p in plans:
                name = p.name.split('.')[0]
                stem = f'{name}.{contract["name"]}'
                src = next(p for p in (EXAMPLES / f'{name}.spt', EXAMPLES / 'fabric' / f'{name}.spt') if p.exists())
                write(contract, src, Path(tmp) / stem, frontend)
                for suffix in ('.hex', '.plan.json', '.mesh.json'):
                    assert (Path(tmp) / (stem + suffix)).read_text() == (committed / (stem + suffix)).read_text(), \
                        f'RTL repository holds a stale {stem}{suffix}: rerun fabric_build.py'
        print(f'RTL repository programs for {contract["name"]} match this compiler: {", ".join(sorted(must))}')
    else:
        print(f'{contract["name"]}: no committed programs in RTL_ROOT: committed-program check skipped')


def static_control(frontend):
    """ACTOR_FABRIC.md A1: machines lowered to registered-LUT state machines
    (static routes, no contexts). Each LUT machine must follow its source
    machine exactly, `transition_clocks` late, whenever events are at least
    that far apart (random single events, every input); a machine whose
    states each read at most four signals takes one clock (as a contexted
    switch); derived registers start at their reset-state values."""
    import random
    from fabric_arch import make_contract
    from fabric_build import model_ir, plan
    from fabric_lower import Lowering, lower
    from fabric_logic import unkey
    T, I, S, C, K, L = 'timer', 'io', 'shifter', 'crc', 'counter', 'logic'
    con = make_contract(3, 5, [L, L, I, L, T, S, C, S, C, I, L, K, L, K, L], [1] * 15, 6, 'a1_static')
    expect = {'i2c_target': {'bus': 3, 'ack': 3}, 'spi': {'open_fsm': 2, 'selected_fsm': 1}, 'swd': {'_mode': 1}}
    for prog, clocks in expect.items():
        src = next(p for p in (EXAMPLES / f'{prog}.spt', EXAMPLES / 'fabric' / f'{prog}.spt') if p.exists())
        cap, orig = {}, Lowering.machines_to_luts

        def hook(self, connect, cap=cap, orig=orig):
            cap['fsms'] = json.loads(json.dumps(self.fsms))
            orig(self, connect)
            cap['luts'] = json.loads(json.dumps(self.luts))
        Lowering.machines_to_luts = hook
        try:
            d = lower(model_ir(frontend, src), prog, plan(frontend, src)['pins'], con, static_control=True)
        finally:
            Lowering.machines_to_luts = orig
        assert not d.get('fsms'), f'{prog}: machines left for contexted switches'
        got = {m: r['transition_clocks'] for m, r in d['control'].items() if m != '_balanced'}
        assert got == clocks, f'{prog}: transition clocks {got}, expected {clocks}'
        fsms, luts = cap['fsms'], cap['luts']
        inputs = sorted({k for l in luts.values() for k in l['leaves'] if not k.startswith('lut:')})
        gap = max(clocks.values())

        def ref(v, cur, env):
            if isinstance(v, int):
                return v
            if isinstance(v, str) and v.startswith('@mux:'):
                v = unkey(v)
            if isinstance(v, dict):
                return ref(v['cases'].get(cur[v['fsm']], v.get('default', 0)), cur, env)
            return env[v]
        q = {n: l['init'] for n, l in luts.items()}
        cur = {m: f['states'][0] for m, f in fsms.items()}
        hist, rng = [], random.Random(7)
        for t in range(3000):
            env = {k: 0 for k in inputs}
            if t % gap == 0:
                env[rng.choice([k for k in inputs if not k.startswith('flag:')])] = 1
            nxt = {}
            for m, f in fsms.items():
                nxt[m] = next((b for a, sig, b in f['on'] if a == cur[m] and ref(sig, cur, env)), cur[m])
            q = {n: (l['truth'] >> sum((q[k[4:]] if k.startswith('lut:') else env[k]) << i
                                         for i, k in enumerate(l['leaves']))) & 1 for n, l in luts.items()}
            hist.append(cur)
            cur = nxt
            for m, f in fsms.items():
                hot = [st for st in f['states'] if q[f'~{m}_{st}']]
                assert len(hot) == 1, f'{prog} {m}: states {hot} at clock {t}'
                if t > gap + 2:
                    want = (hist + [cur])[-clocks[m]][m]
                    assert hot[0] == want, f'{prog} {m}: {hot[0]} at clock {t}, source machine {want}'
        if prog == 'i2c_target':   # SDA released (value 1) in the reset state: no glitch at start
            sda = next(n for n, l in luts.items() if n.startswith('~ack_sel') and l['init'] == 1)
            assert sda
    print('PASS static control (A1): LUT machines follow their machines; SWD one clock, I2C three')


def main():
    frontend = Path(os.environ.get('SPATIAL_FRONTEND', FRONTEND))
    rtl = os.environ.get('RTL_ROOT')
    rtl = Path(rtl) if rtl and (Path(rtl) / 'architecture/grid_5x3.json').exists() else None
    if rtl:
        check_registers(rtl)
    static_control(frontend)
    for contract, must in [(test_mesh(), MUST_COMPILE)] + [(c, MUST_COMPILE_LOGIC - HARDWARE_REFUSED.get(c['name'], set()))
                                                            for c in logic_meshes()]:
        coverage(contract, must, frontend, rtl)
    print('PASS fabric lowering')


if __name__ == '__main__':
    main()
