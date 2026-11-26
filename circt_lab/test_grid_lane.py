#!/usr/bin/env python3
"""Lane designs: assembler, encoder and their agreement with the RTL side."""
import importlib.util
import json
import tempfile
from pathlib import Path

import grid_route
import grid_temporal_image
from grid_contract import DEFAULT
from grid_lane import Lane, build

HERE = Path(__file__).resolve().parent
contract = json.loads(Path(DEFAULT).read_text())
lane = Lane(contract)

# 1. The RTL repository's test assembler and this one agree word for word.
rtl_asm_path = Path(DEFAULT).parents[1] / 'test/cocotb_tests/lane_asm.py'
spec = importlib.util.spec_from_file_location('rtl_lane_asm', rtl_asm_path)
rtl_asm = importlib.util.module_from_spec(spec); spec.loader.exec_module(rtl_asm)
program = '''
start:  CLEAR
        WANY 12
        XOR 8
        JNZ wrong
next:   RD 3
        JZ done
        PULL
        XOR 0x5A
        PUSH
        SHIFT
        FIRE
        ACT 0xB
        WAIT 10
        LDI 0x547
        WCFG 114
        SETC 3
loop:   LOOP loop
        ATOC
        CTOA
        SHL
        SHR
        AND 0xFF
        ADD 1
        J next
done:   HALT
wrong:  FAIL
        NOP
'''
assert lane.assemble(program) == rtl_asm.assemble(program)

# 2. Contract names resolve to the numbers the RTL wires.
named = lane.assemble('''
        WANY bit:event:input0_edge|bit:event:input1_edge
        WAIT event:shifter0_done
        WAIT level:input0_low
        RD readback:stream_bytes_left
        HALT
''')
assert named == lane.assemble('WANY 12\nWAIT 1\nWAIT 10\nRD 3\nHALT')

# 3. Programs the lane would fail on are refused at build time.
for bad, why in [('WCFG 117\nHALT', 'outside'), ('WANY 0\nHALT', 'empty'), ('J 5\nHALT', 'past the program end'),
                 ('a: NOP\na: HALT', 'twice'), ('WAIT 16\nHALT', 'out of range'), ('MOV 1', 'unknown'),
                 ('HALT 1', 'no operand'), ('NOP\n' * 65, 'buffer')]:
    try:
        lane.assemble(bad)
    except ValueError as e:
        assert why in str(e), (bad, str(e))
    else:
        raise AssertionError(f'accepted: {bad!r}')

# 4. A lane image routes exactly like the temporal image and places the lane
#    words where the contract says.
nets = json.loads((HERE / 'profiles/i2c_write_nets.json').read_text())['nets']
with tempfile.TemporaryDirectory() as tmp:
    (Path(tmp) / 'p.lane').write_text('ACT 3\nHALT\n')
    design = {'schema': 'spatial.lane-design.v1', 'name': 'check', 'nets': nets,
              'words': {'68': 3, '4': 0x40}, 'idle_actions': 3, 'program': 'p.lane', 'program_base': 768}
    (Path(tmp) / 'd.json').write_text(json.dumps(design))
    image, prog = build(contract, Path(tmp) / 'd.json')
    routes = grid_route.routes(contract, {'name': 'check', 'nets': nets})
    temporal = grid_temporal_image.encode(contract, routes, {'rows': [0] * 16, 'base_words': design['words'],
                                                            'entry': 0, 'idle_actions': 3})
    L = contract['lane']; cfg = contract['configuration']
    lane_only = {L['mode_word'], L['program_word'], L['idle_actions_word'], *range(cfg['temporal_rows'][0], cfg['temporal_rows'][1] + 1)}
    assert len(image) == len(temporal)
    assert [i for i in range(len(image)) if image[i] != temporal[i] and i not in lane_only] == []
    assert image[L['mode_word']] == 1 << L['mode_bit']
    assert image[L['program_word']] == (1 << 10) | 768
    assert image[L['idle_actions_word']] == 3 << 4
    assert all(image[i] == 0 for i in range(cfg['temporal_rows'][0], cfg['temporal_rows'][1] + 1))
    assert prog['bytes'] == [0x03, 0x20, 0x01, 0x00]  # ACT 3 = 0x2003, HALT = 0x0001, little-endian
    for word in (L['mode_word'], L['program_word']):
        design['words'][str(word)] = 1
        (Path(tmp) / 'd.json').write_text(json.dumps(design))
        try:
            build(contract, Path(tmp) / 'd.json')
        except ValueError as e:
            assert 'set by the lane encoder' in str(e)
        else:
            raise AssertionError(f'design may not set word {word}')
        del design['words'][str(word)]

# 5. Timer modes encode from the contract fields into the timer's own words.
from grid_lane import timer_mode_words
cfg = contract['configuration']
words = timer_mode_words(contract, {'timer0': {'function': 'capture'},
                                    'timer3': {'function': 'resync', 'phase': 10, 'resync_limit': 3}})
m0, p0 = contract['resources']['timer0']['mode_words']; m3, p3 = contract['resources']['timer3']['mode_words']
assert words == {m0: cfg['timer_functions']['capture'], p0: 0,
                 m3: cfg['timer_functions']['resync'] | 3 << cfg['timer_mode_fields']['resync_limit'][1], p3: 10}, words
for bad, why in [({'shifter0': {'function': 'capture'}}, 'not a timer'), ({'timer0': {'function': 'pwm'}}, 'unknown function'),
                 ({'timer0': {'function': 'count', 'phase': 3}}, 'resync only'), ({'timer0': {'function': 'resync'}}, 'phase'),
                 ({'timer0': {'function': 'resync', 'phase': 4, 'resync_limit': 256}}, 'out of range'),
                 ({'timer0': {'mode': 1}}, 'unknown keys')]:
    try:
        timer_mode_words(contract, bad)
    except ValueError as e:
        assert why in str(e), (bad, str(e))
    else:
        raise AssertionError(f'accepted: {bad}')

print('PASS: lane assembler matches the RTL test assembler, names come from the contract, '
      'bad programs are refused, lane images route like temporal images, timer modes encode from the contract')
