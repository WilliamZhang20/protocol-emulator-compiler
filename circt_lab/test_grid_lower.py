#!/usr/bin/env python3
"""R7: general lowering from source. examples/ps2.spt lowers (frontend IR
-> lane design -> image) to exactly the data the RTL pin test
ps2_lowered_from_source runs; constructs a lane core cannot hold are
refused with the reason."""
import json
import os
import subprocess
import tempfile
from pathlib import Path

from grid_contract import DEFAULT
from grid_lane import build
from grid_lower import Lowering, LowerError

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
RTL = Path(DEFAULT).parents[1]
contract = json.loads(Path(DEFAULT).read_text())
spatial = os.environ.get('SPATIAL_FRONTEND', str(ROOT / 'target/debug/spatial'))


def ir(name):
    out = subprocess.run([spatial, 'model-ir', str(ROOT / 'examples' / f'{name}.spt')],
                         check=True, capture_output=True, text=True).stdout
    return json.loads(out)


with tempfile.TemporaryDirectory() as tmp:
    design, program = Lowering(ir('ps2'), count=3, name='ps2_rx').lower()
    (Path(tmp) / 'ps2_rx.lane').write_text(program)
    design['program'] = 'ps2_rx.lane'
    (Path(tmp) / 'ps2_rx.json').write_text(json.dumps(design))
    image, prog = build(contract, Path(tmp) / 'ps2_rx.json')
    data = RTL / 'test/data/lane'
    assert (data / 'ps2-rx.mem').read_text() == ''.join(f'{x:04x}\n' for x in image)
    assert json.loads((data / 'ps2-rx.program.json').read_text()) == json.loads(json.dumps(prog))
    assert design['connect'] == {'coder.start': 'high', 'coder.shift': 'in0.fall', 'coder.sample': 'in0.fall',
                                 'coder.in': 'in1'}
    assert design['coder']['frame'] == {'length': 8, 'pre': 1, 'pre_value': 0, 'post': 1, 'post_value': 1,
                                        'parity': 'odd'}

# Pin-referenced source times: a receive timer started by the input's
# edge and sampling it is written at the pins; the lane design carries it
# as sample_first, and the image equals one built with the edge-to-sample
# offset taken off by hand.
RX = '''clock 50 MHz pin rx: in
timer bit {{ every: 64 cycles, first: {first}, pulse, run: rx.fall until r.done }}
shifter r {{ frame: [0, data(8), 1], start: rx.fall, shift: bit.tick, sample: rx at bit.tick }}
export stream recv: r -> host
'''
TX = '''clock 50 MHz pin tx: out = s.out
timer bit {{ every: 64 cycles, first: {first}, pulse, run: s.busy until s.done }}
shifter s {{ frame: [0, data(8), 1], idle: 1, shift: bit.tick }}
export stream send: host -> s
'''


def ir_of(text):
    with tempfile.NamedTemporaryFile('w', suffix='.spt', delete=False) as f:
        f.write(text)
    try:
        out = subprocess.run([spatial, 'model-ir', f.name], check=True, capture_output=True, text=True).stdout
    finally:
        os.unlink(f.name)
    return json.loads(out)


def image_of(design, program, tmp):
    (Path(tmp) / 'p.lane').write_text(program)
    design = dict(design, program='p.lane')
    (Path(tmp) / 'p.json').write_text(json.dumps(design))
    return build(contract, Path(tmp) / 'p.json')[0]


off = contract['lane_core']['edge_to_sample_cycles']
with tempfile.TemporaryDirectory() as tmp:
    design, program = Lowering(ir_of(RX.format(first='32 cycles')), count=1).lower()
    assert design['timers']['timer0'] == {'period': 64, 'sample_first': 32}, design['timers']
    by_hand = json.loads(json.dumps(design))
    by_hand['timers']['timer0'] = {'period': 64, 'first': 32 - off}
    assert image_of(design, program, tmp) == image_of(by_hand, program, tmp)
    design, program = Lowering(ir_of(RX.format(first=f'{off} cycle')), count=1).lower()
    try:
        image_of(design, program, tmp)
    except ValueError as e:
        assert 'edge-to-sample' in str(e), str(e)
    else:
        raise AssertionError('a pin time inside the edge-to-sample offset was accepted')
# A transmit timer started by the shifter's busy and shifting it: `first`
# is the start bit's width at the pin; the design carries shift_first, and
# the image equals one built with the contract's start-to-shift latency
# taken off by hand. The stream starts the shifter through the
# controller's shift line.
lat = contract['lane_core']['start_to_shift_cycles']
with tempfile.TemporaryDirectory() as tmp:
    design, program = Lowering(ir_of(TX.format(first='64 cycles')), count=4).lower()
    assert design['timers']['timer0'] == {'period': 64, 'shift_first': 64}, design['timers']
    assert design['connect'] == {'timer0.start': 'coder.active', 'timer0.stop': 'coder.stop',
                                 'coder.shift': 'timer0.tick', 'coder.start': 'shift',
                                 'out0.value': 'coder.serial', 'out0.enable': 'high'}, design['connect']
    assert 'PULL' in program and 'SHIFT' in program and 'SETC 4' in program
    by_hand = json.loads(json.dumps(design))
    by_hand['timers']['timer0'] = {'period': 64, 'first': 64 - lat}
    assert image_of(design, program, tmp) == image_of(by_hand, program, tmp)
    design, program = Lowering(ir_of(TX.format(first=f'{lat} cycles')), count=4).lower()
    try:
        image_of(design, program, tmp)
    except ValueError as e:
        assert 'start-to-shift' in str(e), str(e)
    else:
        raise AssertionError('a start bit inside the start-to-shift latency was accepted')

# The transmitter written at the pins (lanes/uart_tx_8n1.spt) lowers to
# exactly the data the RTL pin test uart_tx_lowered_from_source runs, which
# checks every bit is BIT clocks wide at the pin.
with tempfile.TemporaryDirectory() as tmp:
    out = subprocess.run([spatial, 'model-ir', str(HERE / 'lanes/uart_tx_8n1.spt')],
                         check=True, capture_output=True, text=True).stdout
    design, program = Lowering(json.loads(out), count=4, name='uart_tx_lowered').lower()
    (Path(tmp) / 'uart_tx_lowered.lane').write_text(program)
    design['program'] = 'uart_tx_lowered.lane'
    (Path(tmp) / 'uart_tx_lowered.json').write_text(json.dumps(design))
    image, prog = build(contract, Path(tmp) / 'uart_tx_lowered.json')
    data = RTL / 'test/data/lane'
    assert (data / 'uart-tx-lowered.mem').read_text() == ''.join(f'{x:04x}\n' for x in image)
    assert json.loads((data / 'uart-tx-lowered.program.json').read_text()) == json.loads(json.dumps(prog))
    assert design['connect'] == json.loads((HERE / 'lanes/uart_tx_lowered.json').read_text())['connect']

for name, why in [('uart', '2 shifters'), ('i2c', '2 shifters'), ('usb', 'counter'), ('spi', 'counter')]:
    try:
        Lowering(ir(name), count=3).lower()
    except LowerError as e:
        assert why in str(e), (name, str(e))
    else:
        raise AssertionError(f'{name} lowered to one lane core')
try:
    Lowering(ir('ps2')).lower()
except LowerError as e:
    assert 'word count' in str(e), str(e)
else:
    raise AssertionError('receive program without a count')
print('PASS: examples/ps2.spt lowers by rule to the image the RTL pin test runs; a pin-measured receive '
      'timer lowers to sample_first and a transmit timer to shift_first (with its pull/shift program), each '
      'building the image of the hand-offset count; times inside either latency are refused; uart and i2c (two shifters), usb and spi (counters) are '
      'refused with the reason')
