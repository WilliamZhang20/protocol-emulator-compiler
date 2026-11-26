#!/usr/bin/env python3
"""Lane designs (schema v2, the lane core): assembler, encoder and their
agreement with the RTL side."""
import importlib.util
import json
import tempfile
from pathlib import Path

from grid_contract import DEFAULT
from grid_lane import Lane, CoreEncoder, build

HERE = Path(__file__).resolve().parent
RTL = Path(DEFAULT).parents[1]
contract = json.loads(Path(DEFAULT).read_text())
core = contract['lane_core']
L = contract['lane']
lines = {name: index for index, name, _ in core['lines']}
selectors = {name: (word, bits, kind) for name, word, bits, kind in core['selectors']}


def field(words, selector):
    word, (hi, lo), _ = selectors[selector]
    return (words[word] >> lo) & ((1 << (hi - lo + 1)) - 1)


def refused(fn, why):
    try:
        fn()
    except ValueError as e:
        assert why in str(e), (why, str(e))
    else:
        raise AssertionError(f'accepted, expected {why!r}')


# 1. The RTL repository's test assembler and this one agree word for word.
spec = importlib.util.spec_from_file_location('rtl_lane_asm', RTL / 'test/cocotb_tests/lane_asm.py')
rtl_asm = importlib.util.module_from_spec(spec); spec.loader.exec_module(rtl_asm)
program = '''
start:  CLEAR
        WANY 12
        XOR 8
        JNZ wrong
next:   RD 7
        JZ done
        PULL
        XOR 0x5A
        PUSH
        SHIFT
        FIRE
        ACT 0xB
        WAIT 10
        LDI 0x547
        WCFG 1
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
lane = Lane(contract)
assert lane.assemble(program) == rtl_asm.assemble(program)
# ... and the RTL test helper encodes connections like the compiler.
connect = {'timer0.start': 'fire', 'coder.in': 'in1', 'out0.value': 'timer0.level', 'out0.enable': 'high',
           'crc.absorb_gate': 'coder.active'}
words = CoreEncoder(contract).encode({}, connect)
helper = rtl_asm.core_words(rtl_asm.assemble('HALT'), 768, connect)
base = L['core_page'] << 8
assert all(helper[base + i] == words[i] for i in range(core['words']) if i != L['program_word']), (helper, words)

# 2. Names resolve to the slots and numbers the core wires.
lane = Lane(contract)
source = '''
        WANY bit:event:in0.rise|bit:event:coder.word
        WAIT event:coder.word
        WAIT level:in1
        RD readback:stream_bytes_left
        WCFG word:timer1.period
        HALT
'''
allocated = lane.allocate(source, {'event5': 'in0.rise'})
assert allocated == {'event5': 'in0.rise', 'event0': 'coder.word', 'level0': 'in1'}, allocated
rb = core['readback'].index('stream_bytes_left')
assert lane.assemble(source) == lane.assemble(f'WANY {1 << 5 | 1}\nWAIT 0\nWAIT 8\nRD {rb}\nWCFG 5\nHALT')
refused(lambda: Lane(contract).allocate('WAIT event:nosuch', {}), 'no such line')
many = '\n'.join(f'WAIT event:{name}' for _, name, _ in core['lines'][:9])
refused(lambda: Lane(contract).allocate(many, {}), 'more than 8 event lines')

# 3. Programs the lane would fail on are refused at build time.
for bad, why in [('WCFG 2\nHALT', 'outside'), ('WCFG 112\nHALT', 'outside'), ('WCFG word:timer0.mode\nHALT', 'not a live'),
                 ('WANY 0\nHALT', 'empty'), ('J 5\nHALT', 'past the program end'), ('a: NOP\na: HALT', 'twice'),
                 ('WAIT 16\nHALT', 'out of range'), ('MOV 1', 'unknown'), ('HALT 1', 'no operand'),
                 ('NOP\n' * 65, 'buffer')]:
    refused(lambda: lane.assemble(bad), why)

# 4. Connections, timers and registers land where the contract says.
design = {
    'timers': {'timer0': {'first': 8, 'period': 20, 'function': 'resync', 'phase': 8, 'resync_limit': 4},
               'timer1': {'function': 'capture'}},
    'coder': {'frame': {'length': 8, 'pre': 1, 'post': 1, 'post_value': 1, 'parity': 'odd', 'idle': 1},
              'io': {'auto_push': 1}, 'coder': {'stuff_run': 5, 'runs_of_ones': 1}},
    'crc': {'polynomial': 0x04C11DB7, 'residue': 0xC704DD7B, 'shape': {'width': 32, 'init_ones': 1}},
    'idle_actions': 5,
}
connect = {'timer0.start': 'in0.fall', 'timer0.aux': 'in0.fall', 'coder.in': 'in0', 'coder.shift': 'timer0.tick',
           'crc.init': 'coder.stop', 'crc.emit_gate': 'coder.active', 'out1.value': 'low', 'out1.enable': 'action2'}
words = CoreEncoder(contract).encode(design, connect)
for selector, line in connect.items():
    want = lines[line] - (core['level_lines'][0] if selectors[selector][2] == 'level' else 0)
    assert field(words, selector) == want, selector
gw, gb = core['gate_enables']['crc.emit_gate']
assert words[gw] >> gb & 1
layout = core['word_layout']
cfg = contract['configuration']
t0 = layout['timer0'][0]
assert words[t0:t0 + 4] == [8, 20, cfg['timer_functions']['resync'] | 4 << cfg['timer_mode_fields']['resync_limit'][1], 8]
t1 = layout['timer1'][0]
assert words[t1:t1 + 4] == [0, 0, cfg['timer_functions']['capture'], 0]
frame = core['registers']['coder.frame']
assert words[frame['word']] == 7 | 1 << 4 | 1 << 7 | 1 << 9 | 2 << 10 | 1 << 15
assert words[core['registers']['coder.io']['word']] >> 12 & 1
cf = core['registers']['coder.coder']['fields']
assert words[core['registers']['coder.coder']['word']] == 5 | 1 << cf['runs_of_ones'][1]
lo, hi = core['registers']['crc.polynomial']['words']
assert (words[lo], words[hi]) == (0x1DB7, 0x04C1)
assert words[core['registers']['crc.shape']['word']] == 31 | 1 << 5
c2 = core['registers']['coder.coder2']
w = CoreEncoder(contract).encode({'coder': {'coder2': {'link_data_only': 1, 'crc_skip': 3}}}, {})
assert w[c2['word']] == 1 << c2['fields']['link_data_only'][1] | 3 << c2['fields']['crc_skip'][1]
assert words[L['idle_actions_word']] == 5 << L['idle_actions_bits'][1]

# 5. Configurations the core would refuse are refused here.
enc = CoreEncoder(contract)
for bad_design, bad_connect, why in [
        ({}, {'coder.in': 'in0.fall'}, 'takes a level line'),
        ({}, {'crc.init': 'in0'}, 'takes a pulse line'),
        ({}, {'nosuch.input': 'in0'}, 'no selector'),
        ({}, {'timer0.start': 'nosuch'}, 'no such line'),
        ({'timers': {'timer0': {'first': 5}}}, {}, 'set together'),
        ({'timers': {'timer2': {'first': 5, 'period': 5}}}, {}, 'no timer'),
        ({'timers': {'timer0': {'function': 'count', 'phase': 3}}}, {}, 'resync only'),
        ({'coder': {'coder': {'stuff_run': 1}}}, {}, 'needs runs_of_ones'),
        ({'coder': {'coder': {'manchester': 1, 'nrzi': 1}}}, {}, 'excludes NRZI'),
        ({'coder': {'frame': {'parity': 'mark'}}}, {}, 'parity'),
        ({'coder': {'frame': {'length': 17}}}, {}, '1-16'),
        ({'coder': {'frame': {'pre': 4}}}, {}, 'does not fit'),
        ({'crc': {'shape': {'twos_complement': 1}}}, {}, 'needs additive'),
        ({'crc': {'shape': {'link_select': 1}}}, {}, 'one coded shifter'),
        ({'crc': {'shape': {'linked': 1}}}, {'coder.sample': 'timer0.tick'}, 'needs coder.coder.receive'),
        ({'coder': {'coder2': {'link_data_only': 2}}}, {}, 'does not fit'),
        ({'idle_actions': 16}, {}, 'does not fit')]:
    refused(lambda: enc.encode(bad_design, bad_connect), why)

# 6. A whole design: the page-0 image is the inert grid in lane mode, page 1
#    the core; v1 designs are refused with a pointer to v2.
with tempfile.TemporaryDirectory() as tmp:
    (Path(tmp) / 'p.lane').write_text('WAIT event:in0.rise\nACT 3\nHALT\n')
    d = {'schema': 'spatial.lane-design.v2', 'name': 'check', 'connect': {'out0.value': 'action0'},
         'host_words': {'68': 3}, 'idle_actions': 1, 'program': 'p.lane', 'program_base': 768}
    (Path(tmp) / 'd.json').write_text(json.dumps(d))
    image, prog = build(contract, Path(tmp) / 'd.json')
    expect = [0] * (cfg['alternate_context_words'][1] + 1)
    for k, v in L['inert_grid_words'].items():
        expect[int(k)] = v
    expect[68] = 3
    expect[L['mode_word']] = 1 << L['mode_bit']
    assert image == expect
    cw = {int(k): v for k, v in prog['core_words'].items()}
    assert len(cw) == core['words'] and prog['core_page'] == L['core_page']
    assert cw[L['program_word']] == 2 << 10 | 768
    assert prog['connect'] == {'event0': 'in0.rise', 'out0.value': 'action0'}
    assert prog['bytes'] == [0x00, 0x10, 0x03, 0x20, 0x01, 0x00]   # WAIT 0, ACT 3, HALT
    d['host_words'] = {'91': 1}
    (Path(tmp) / 'd.json').write_text(json.dumps(d))
    refused(lambda: build(contract, Path(tmp) / 'd.json'), 'not a host word')
    d['host_words'] = {}
    d['connect'] = {'event0': 'in1.rise'}
    (Path(tmp) / 'd.json').write_text(json.dumps(d))
    image, prog = build(contract, Path(tmp) / 'd.json')   # event0 is in1.rise: the program's line gets slot 1
    assert prog['connect'] == {'event0': 'in1.rise', 'event1': 'in0.rise'} and prog['bytes'][:2] == [0x01, 0x10]
    del d['connect']
    d['schema'] = 'spatial.lane-design.v1'
    (Path(tmp) / 'd.json').write_text(json.dumps(d))
    refused(lambda: build(contract, Path(tmp) / 'd.json'), 'lane-design.v2')

# 7. The shipped designs build to exactly the data the RTL pin tests load.
data = RTL / 'test/data/lane'
for name in ('i2c_target', 'spi_target', 'slip_uart_tx', 'pulse_period', 'uart_rx_resync', 'uart_rx_plain',
             'uart_rx_8e1', 'manchester_rx', 'lin_checksum'):
    image, prog = build(contract, HERE / 'lanes' / f'{name}.json')
    stem = name.replace('_', '-')
    assert (data / f'{stem}.mem').read_text() == ''.join(f'{x:04x}\n' for x in image), name
    assert json.loads((data / f'{stem}.program.json').read_text()) == json.loads(json.dumps(prog)), name

print('PASS: lane assembler and core helper match the RTL side, names allocate slots from the contract, '
      'bad programs and configurations are refused, core words land where the contract says, '
      'shipped designs match the RTL pin-test data')
