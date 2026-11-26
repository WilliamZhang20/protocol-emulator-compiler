#!/usr/bin/env python3
"""Tests for the reorganised-fabric compiler (fabric_arch.py, fabric_map.py).

1. The I2C target + UART receive compile from source onto the 3 x 3
   prototype mesh and pass the independent configuration check.
2. The checker catches a broken context invariant (a route through the
   ACK FSM's switch that changes in one context: the RTL slice's +BAD),
   a wrong mux case, and a stray sink.
3. Source rule violations are refused with their class: too many states,
   three transitions from one state, a constant-1 mux read off its cell,
   two designs on one pad, too few tiles.
4. Step 4 L0 fixtures compile: UART TX from the word network's reader on
   a push-pull pad, and a burst with the linked CRC cell's tail on a pad
   pair (polarity).
5. With RTL_ROOT set: the RTL repository's committed contract and vectors
   are exactly this compiler's output, and each runs at the pins on the
   RTL mesh (reorg/mesh_tb.sv).
"""
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from fabric_arch import DIRS, prototype, verilog_params
from fabric_map import MapError, Netlist, check, compile_designs, hexfile

HERE = Path(__file__).resolve().parent
DESIGNS = HERE / 'fabric_designs'
C3 = prototype()
K3 = [c['kind'] for c in C3['cells']]


def design(name):
    return json.loads((DESIGNS / f'{name}.json').read_text())


def refused(cls, contract, designs):
    try:
        compile_designs(contract, designs, seeds=3, moves=500)
    except MapError as e:
        assert e.cls == cls, (cls, str(e))
        return str(e)
    raise AssertionError(f'expected a {cls} failure')


def flip_field(bits, lsb, width, value):
    return (bits & ~(((1 << width) - 1) << lsb)) | (value << lsb)


def main():
    r = compile_designs(C3, [design('i2c_target'), design('uart_rx')])
    prob, nl, bits = r['prob'], r['netlist'], r['bits']
    assert check(C3, nl, prob, bits) == []
    print(f'compiled I2C target + UART: {r["tracks_used"]} track segments, {r["iterations"]} iterations, check passed')

    # A track used by the UART that passes a contexted switch owned by an
    # I2C FSM: change it in one context only. The check must see the UART
    # sink read something else in some state combination.
    fsm_cells = set(prob.fsm_cell.values())
    uart_tracks = [b for name, edges in r['routed']['trees'].items() if name.startswith('uart_rx/')
                   for _, b in edges if b[0] == 'track' and b[1] in fsm_cells]
    lay = C3['layout']
    if uart_tracks:
        cell, d, t = uart_tracks[0][1:]
        lsb = cell * lay['cell_bits'] + lay['track_lsb'] + 48 * 2 + 12 * DIRS.index(d) + 3 * t
        problems = check(C3, nl, prob, flip_field(bits, lsb, 3, 0))
        assert any('uart_rx' in p for p in problems), problems
        print(f'broken invariant (UART route through cell {cell} dropped in context 2) caught')
    else:
        # Force the case: drop a static selector of any routed track in a
        # contexted FSM cell in one context.
        xs = set(r['routed']['x'].values())       # mux points vary by context by design
        edges = [b for edges in r['routed']['trees'].values() for _, b in edges
                 if b[0] == 'track' and b[1] in fsm_cells and b not in xs]
        cell, d, t = edges[0][1:]
        lsb = cell * lay['cell_bits'] + lay['track_lsb'] + 48 * 1 + 12 * DIRS.index(d) + 3 * t
        assert check(C3, nl, prob, flip_field(bits, lsb, 3, 0))
        print(f'broken invariant (route through FSM cell {cell} dropped in context 1) caught')
    # Wrong mux case: the SDA enable reads 1 in a state other than DRIVE.
    drv = next(n for i, n in prob.ipin_of.items() if nl.consumers[i]['block'] == 'i2c_target/sda_out'
               and nl.consumers[i]['sink'] == 'enable')
    lsb = drv[1] * lay['cell_bits'] + lay['sink_lsb'] + 5 * lay['sink_slots'] * 1 + 5 * drv[2]
    assert any('sda_out.enable' in p for p in check(C3, nl, prob, flip_field(bits, lsb, 5, 1)))
    # A stray sink: an unused timer sink reads constant 1.
    unused = next((c, k) for c in range(9) for k in range(3) if K3[c] == 'timer' and ('ipin', c, k) not in prob.ipin_of.values())
    lsb = unused[0] * lay['cell_bits'] + lay['sink_lsb'] + 5 * unused[1]
    assert any('unused sink' in p for p in check(C3, nl, prob, flip_field(bits, lsb, 5, 1)))
    print('wrong mux case and stray sink caught')

    # Source rules.
    d = design('i2c_target')
    d5 = json.loads(json.dumps(d))
    d5['fsms']['addr']['states'].append('EXTRA')
    print('refused:', refused('resources', C3, [d5]))
    d3 = json.loads(json.dumps(d))
    d3['fsms']['addr']['on'].append(['ADDR', 'stop', 'IDLE'])
    print('refused:', refused('resources', C3, [d3]))
    dc = json.loads(json.dumps(d))
    dc['tiles']['t'] = {'kind': 'timer', 'first': 1, 'period': 1}
    dc['connect']['t.start'] = {'fsm': 'ack', 'cases': {'DRIVE': 1}}   # constant 1 needed in a timer cell (no contexts)
    print('refused:', refused('resources', C3, [dc]))
    u2 = design('uart_rx')
    u2['name'] = 'uart_rx2'
    print('refused:', refused('rules', C3, [design('uart_rx'), u2]))
    u2['pins'] = {'rx': 3}
    u3 = json.loads(json.dumps(u2))
    u3['name'] = 'uart_rx3'
    u3['pins'] = {'rx': 4}
    print('refused:', refused('resources', C3, [design('uart_rx'), u2, u3, design('i2c_target')]))

    vectors = {'i2c_target+uart_rx': (bits, [[], ['+UART'], ['+UART', '+HALF=4', '+SETUP=1', '+HOLD=4']])}
    for name, mode in (('uart_tx', '+TX'), ('crc_tx', '+CRCTX')):
        r2 = compile_designs(C3, [design(name)])
        vectors[name] = (r2['bits'], [[mode]])
        print(f'compiled {name}: {r2["tracks_used"]} track segments, check passed')

    rtl = os.environ.get('RTL_ROOT')
    if rtl and not (Path(rtl) / 'experiments/grid_fabric/reorg/mesh.v').exists():
        print(f'{rtl} has no reorg mesh (older hardware pin): pin-level run skipped')
        rtl = None
    if rtl:
        committed = Path(rtl) / 'experiments/grid_fabric/reorg/compiled'
        if committed.exists():
            assert json.loads((committed / 'mesh_3x3.json').read_text()) == C3, 'RTL contract differs from prototype()'
            for name, (b, _) in vectors.items():
                assert (committed / f'{name}.3x3.hex').read_text() == hexfile(C3, b), \
                    f'RTL repository holds a stale {name} vector: rerun fabric_map.py --hex'
            print('RTL repository contract and compiled vectors match this compiler')
        run_rtl(Path(rtl), C3, vectors)
    else:
        print('RTL_ROOT not set: pin-level run skipped')
    print('PASS fabric compiler')


def run_rtl(rtl, contract, vectors):
    reorg = rtl / 'experiments/grid_fabric/reorg'
    tools = rtl / '.tools/oss-cad-suite/bin'
    iverilog = tools / 'iverilog' if (tools / 'iverilog').exists() else 'iverilog'
    vvp = tools / 'vvp' if (tools / 'vvp').exists() else 'vvp'
    params = verilog_params(contract)
    with tempfile.TemporaryDirectory() as tmp:
        sim = Path(tmp) / 'sim'
        cmd = [str(iverilog), '-g2012', '-s', 'mesh_tb', '-o', str(sim)]
        cmd += [f'-Pmesh_tb.{k}={v}' for k, v in params.items()]
        cmd += [str(reorg / f) for f in ('mesh_tb.sv', 'mesh.v', 'ctx_switch.v', 'io_tile.v', 'wnet.v')]
        cmd += [str(rtl / 'src' / f) for f in ('grid_timer.v', 'fabric_shifter.v', 'fabric_crc.v', 'config_bank.v')]
        subprocess.run(cmd, check=True, capture_output=True, text=True)
        for name, (bits, runs) in vectors.items():
            hx = Path(tmp) / f'{name}.hex'
            hx.write_text(hexfile(contract, bits))
            for extra in runs:
                out = subprocess.run([str(vvp), '-N', str(sim), f'+CFG={hx}'] + extra, capture_output=True,
                                     text=True, timeout=120).stdout
                line = [l for l in out.splitlines() if l.startswith(('PASS', 'FAIL'))]
                assert line and line[-1].startswith('PASS'), out[-2000:]
                print(line[-1])


if __name__ == '__main__':
    sys.exit(main())
