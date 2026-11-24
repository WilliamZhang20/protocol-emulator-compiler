#!/usr/bin/env python3
"""Check the emitted grid image against the RTL port/track contract.

Independent of the mapper's search: trace each reported route through the
actual selector words, reject collisions and require the protocol's sinks.
"""
import json
import pathlib
import sys

kind, image_path, report_path = sys.argv[1:]
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
assert len(words) == 66
routes = report['routing']
assert len(routes) == len(expected) and {r['signal'] for r in routes} == set(expected)
occupied = {}
for route in routes:
    name = route['signal']
    source, port, destinations = expected[name]
    lane = route['track']
    assert (route['source_cell'], route['source_port']) == (source, port)
    assert 0 <= lane < 4
    assert {(p['cell'], p['sink']) for p in route['paths']} == destinations
    for path in route['paths']:
        cell, previous = source, None
        for hop in path['hops']:
            assert hop['from'] == cell
            direction, nxt = hop['direction'], hop['to']
            assert 0 <= direction < 4
            assert nxt == cell + (-3, 3, -1, 1)[direction] and 0 <= nxt < 9
            assert direction < 2 or cell // 3 == nxt // 3
            selection = (words[5 + cell * 4 + direction] >> (lane * 3)) & 7
            expected_selection = (1 + port if previous is None else
                                  (6 if previous == 3 else 7) if direction < 2 and previous >= 2 else 5)
            assert selection == hop['selector'] == expected_selection
            # A track has one driver, though a signal may fan out.
            key = cell, direction, lane
            assert key not in occupied or occupied[key] == name
            occupied[key] = name
            cell, previous = nxt, direction
        assert cell == path['cell']
        sink = path['sink']
        addr = 41 + sink if sink < 12 else 56 + sink - 12
        selected = 2 + port if previous is None else (10, 6, 18, 14)[previous] + lane
        assert words[addr] == selected
        if sink in (10, 11):
            assert selected in (0, 1, 14, 15, 16, 17), 'LUT must be feed-forward'
print(f'PASS: {kind} grid image routes {len(routes)} signals without track collisions')
