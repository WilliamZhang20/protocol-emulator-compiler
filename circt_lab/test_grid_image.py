#!/usr/bin/env python3
"""Check the emitted grid image against the RTL port/track contract.

Independent of the mapper's search: trace each reported route through the
actual selector words, reject collisions and require the protocol's sinks.
"""
import json
import os
import pathlib
import sys

kind, image_path, report_path = sys.argv[1:]
rtl_root = pathlib.Path(os.environ.get('RTL_ROOT', pathlib.Path(__file__).resolve().parents[2] / 'protocol-emulator'))
contract = json.loads((rtl_root / 'architecture/grid_3x3.json').read_text())
grid, network = contract['grid'], contract['bit_network']
assert contract['schema'] == 'protocol-emulator.grid-contract.v1'
words = [int(x, 16) for x in pathlib.Path(image_path).read_text().splitlines()]
report = json.loads(pathlib.Path(report_path).read_text())
expected = {
    'spi': {
        'start': (6, 0, {(2, 0), (3, 3)}), 'shift_done': (3, 1, {(2, 1)}),
        'timer_fall': (2, 2, {(3, 4)}), 'timer_rise': (2, 1, {(3, 5)}),
        'miso': (0, 0, {(3, 6)}), 'mosi': (3, 2, {(1, 7)}),
        'sck': (2, 3, {(1, 8)}), 'chip_select': (4, 0, {(1, 9)}),
        'busy': (3, 0, {(4, 10)}),
    },
    'uart': {
        'tx_start': (6, 0, {(2, 0), (3, 3)}), 'tx_done': (3, 1, {(2, 1)}),
        'tx_tick': (2, 0, {(3, 4)}), 'tx_wire': (3, 3, {(1, 7)}),
        'rx_fall': (0, 3, {(5, 12), (7, 15)}), 'rx_done': (7, 1, {(5, 13)}),
        'rx_tick': (5, 0, {(7, 16), (7, 17)}), 'rx_wire': (0, 1, {(7, 18)}),
    },
}[kind]
cell_resource = {0:'input_io', 1:'output_io', 2:'timer0', 3:'shifter0', 4:'lut',
                 5:'timer1', 6:'sram0', 7:'shifter1', 8:'crc'}
def relocated(cell):
    return contract['resources'][cell_resource[cell]]['cell']
expected = {name:(relocated(source), port, {(relocated(cell), sink) for cell, sink in sinks})
            for name, (source, port, sinks) in expected.items()}
assert len(words) == 66
routes = report['routing']
assert len(routes) == len(expected) and {r['signal'] for r in routes} == set(expected)
occupied = {}
for route in routes:
    name = route['signal']
    source, port, destinations = expected[name]
    lane = route['track']
    assert (route['source_cell'], route['source_port']) == (source, port)
    assert 0 <= lane < network['tracks_per_direction']
    assert {(p['cell'], p['sink']) for p in route['paths']} == destinations
    for path in route['paths']:
        cell, previous = source, None
        for hop in path['hops']:
            assert hop['from'] == cell
            direction, nxt = hop['direction'], hop['to']
            assert 0 <= direction < 4
            cols = grid['columns']
            assert nxt == cell + (-cols, cols, -1, 1)[direction] and 0 <= nxt < len(grid['cells'])
            assert direction < 2 or cell // cols == nxt // cols
            switch = network['switch_words']
            selection = (words[switch['first'] + cell * switch['words_per_cell'] + direction] >> (lane * switch['selector_bits'])) & ((1 << switch['selector_bits']) - 1)
            turning = previous is not None and (direction < 2) != (previous < 2)
            if turning:
                classes = network['turn_classes']
                assert (lane in classes[0]['tracks'] and direction < 2) or (lane in classes[1]['tracks'] and direction >= 2)
            values = network['selector_values']
            expected_selection = (values['local_first'] + port if previous is None else
                                  (values['turn_first'] if previous in (1, 3) else values['turn_last']) if turning else values['straight'])
            assert selection == hop['selector'] == expected_selection
            # A track has one driver, though a signal may fan out.
            key = cell, direction, lane
            assert key not in occupied or occupied[key] == name
            occupied[key] = name
            cell, previous = nxt, direction
        assert cell == path['cell']
        sink = path['sink']
        assert network['sink_cells'][sink] == cell
        split = network['sink_word_split']
        addr = network['sink_word_first'] + sink if sink < split else network['sink_word_second'] + sink - split
        sel = network['input_selector']
        selected = sel['local_first'] + port if previous is None else (sel['south_first'], sel['north_first'], sel['east_first'], sel['west_first'])[previous] + lane
        assert words[addr] == selected
        if sink in network['lut_sinks'] and not network['lut_local_feedback']:
            assert selected in (sel['constant_zero'], sel['constant_one']) or sel['north_first'] <= selected <= sel['last'], 'LUT must be feed-forward'
print(f'PASS: {kind} grid image routes {len(routes)} signals without track collisions')
