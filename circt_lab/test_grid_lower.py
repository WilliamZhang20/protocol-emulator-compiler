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
    assert 'word count' in str(e)
else:
    raise AssertionError('receive program without a count')
print('PASS: examples/ps2.spt lowers by rule to the image the RTL pin test runs; uart and i2c (two shifters), '
      'usb and spi (counters) are refused with the reason')
