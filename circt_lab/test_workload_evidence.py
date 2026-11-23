#!/usr/bin/env python3
"""Check that workload evidence follows source changes, without claiming fit."""
import argparse
import json
from pathlib import Path
import subprocess
import sys
import tempfile

HERE = Path(__file__).resolve().parent


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--frontend', required=True)
    p.add_argument('--mapper', required=True)
    args = p.parse_args()
    def extract(*paths):
        cmd = [sys.executable, str(HERE/'investigate_workloads.py'), '--frontend', args.frontend, '--mapper', args.mapper, *map(str, paths)]
        return json.loads(subprocess.run(cmd, text=True, capture_output=True, check=True).stdout)['programs']
    i2c, jtag = extract(HERE.parent/'examples/i2c.spt', HERE.parent/'examples/jtag.spt')
    assert {x['mode'] for x in i2c['pins']} == {'open_drain'}
    assert any(x['consumer']=='byte_clock' and x['port']=='timer.hold' for x in i2c['connections'])
    assert any(x['consumer']=='cond_clock' and x['port']=='timer.hold' for x in i2c['connections'])
    assert any(x['name']=='condition' for x in i2c['functions'])
    byte = next(x for x in i2c['functions'] if x['name']=='byte')
    setting = next(x for x in byte['body'] if x['op']=='set' and x['target']['name']=='sda_bits')
    sample = next(x['value'] for x in setting['fields'] if x['name']=='sample')
    assert sample['op']=='at' and sample['left']['name']=='sda' and sample['right']['object']['name']=='scl' and sample['right']['field']=='rise'
    assert i2c['shift_block_comparison']['combined_shared_event_hypothesis'].startswith('Not established')
    shifts = [x for x in jtag['shared_direct_inputs'] if x['port']=='shifter.shift']
    assert len(shifts)==1 and shifts[0]['consumers']==['data','mode'] and shifts[0]['output']=='fall'
    assert jtag['shift_block_comparison']['combined_shared_event_hypothesis'].startswith('All declared')
    assert all(r['bit_width']==1 for c in jtag['connections'] for r in c['references'] if r['signal_kind'] in ('level','pulse'))
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp)/'changed.spt'
        jtag_mlir = Path(tmp)/'jtag.mlir'
        jtag_mlir.write_text(subprocess.run([args.frontend, 'frontend-mlir', str(HERE.parent/'examples/jtag.spt')], text=True, capture_output=True, check=True).stdout)
        def analyze(candidate):
            return subprocess.run([args.mapper, '--analyze', str(HERE/'investigations'/candidate), str(jtag_mlir)], text=True, capture_output=True)
        separate = analyze('jtag-separate-routes.mlir')
        assert separate.returncode == 0, separate.stderr
        routes = json.loads(separate.stdout)
        assert routes['frontend_dependency_only'] and not routes['hardware_fidelity_verified']
        for sink in ('data.shifter.shift', 'mode.shifter.shift'):
            assert any(x['sink']==sink and x['source']=='clock.fall' and x['selector_hops']==2 for x in routes['assignments'])
        shared = analyze('jtag-shared-routes.mlir')
        assert shared.returncode != 0 and 'no compatible selector assignment' in shared.stderr
        source = (HERE.parent/'examples/jtag.spt').read_text()
        assert 'shifter mode { start: go, shift: clock.fall }' in source
        path.write_text(source.replace('shifter mode { start: go, shift: clock.fall }', 'shifter mode { start: go, shift: clock.rise }'))
        changed, = extract(path)
        assert not any(x['port']=='shifter.shift' for x in changed['shared_direct_inputs'])
        assert any(c['consumer']=='mode' and c['port']=='shifter.shift' and c['references'][0]['output']=='rise' for c in changed['connections'])
    print('PASS: I2C and JTAG source evidence preserves I/O, timing dependencies and mutations')


if __name__ == '__main__':
    main()
