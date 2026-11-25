#!/usr/bin/env python3
"""Encode a routed two-context temporal program into grid config words."""
import argparse
import json
from pathlib import Path

from grid_contract import DEFAULT
from grid_coded_image import switch_address


def encode(contract, routes, program):
    net = contract['bit_network']; cfg = contract['configuration']; temporal = contract['temporal']
    if routes['status'] != 'routed' or routes['track_count'] != net['tracks_per_direction'] or routes['registered_hops'] or routes['direct_links']:
        raise ValueError('requires the current unregistered grid routes')
    if any(routes['layout'][name] != resource['cell'] for name, resource in contract['resources'].items()):
        raise ValueError('route placement does not match RTL contract')
    rows = program['rows']
    if len(rows) != temporal['rows'] or any(not isinstance(x, int) or not 0 <= x <= 0xffff for x in rows):
        raise ValueError('temporal table must have sixteen 16-bit rows')
    words = [0] * (cfg['alternate_context_words'][1] + 1)
    for key, value in program['base_words'].items():
        address = int(key)
        if address >= len(words) or not 0 <= value <= 0xffff:
            raise ValueError('invalid base configuration')
        words[address] = value
    # Clear the original nine cells' switch words; extension cells (words
    # 128+) are outside this image and stay at their reset value of zero.
    base_cells = net['switch_words'].get('extension_first_cell', len(contract['grid']['cells']))
    for i in range(net['switch_words']['first'], net['switch_words']['first'] + base_cells * net['switch_words']['words_per_cell']):
        words[i] = 0
    sink_select = {}
    for path in routes['routes']:
        context = path['sink'].get('context', 0)
        sink = path['sink']['port']
        if sink >= net.get('context_overridable_sinks', len(net['sink_cells'])):
            raise ValueError('only the original 19 tile inputs are supported by this encoder')
        key = context, sink
        if key in sink_select:
            raise ValueError('duplicate sink')
        sink_select[key] = path['sink_select']
        for hop in path['hops']:
            if hop['from'] >= base_cells:
                raise ValueError('extension cells are not supported by this encoder')
            addr = switch_address(net, hop['from'], hop['direction'])
            shift = path['track'] * net['switch_words']['selector_bits']
            words[addr] = (words[addr] & ~(7 << shift)) | (hop['selector'] << shift)
    overrides = []
    for sink in range(net.get('context_overridable_sinks', len(net['sink_cells']))):
        addr = net['sink_word_first'] + sink if sink < net['sink_word_split'] else net['sink_word_second'] + sink - net['sink_word_split']
        base = sink_select.get((0, sink), words[addr])
        alternate = sink_select.get((1, sink), base)
        words[addr] = base
        if alternate != base:
            overrides.append(0x400 | (alternate << 5) | sink)
    if len(overrides) > temporal['alternate_context_slots']:
        raise ValueError('too many alternate-context sink selections')
    for i, word in enumerate(overrides):
        words[cfg['alternate_context_words'][0] + i] = word
    words[cfg['temporal_mode_word']] = 1
    words[cfg['temporal_entry_idle_word']] = program['entry'] | (program['idle_actions'] << 4)
    for i, row in enumerate(rows):
        words[cfg['temporal_rows'][0] + i] = row
    return words


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('routes', type=Path); p.add_argument('program', type=Path); p.add_argument('output', type=Path)
    p.add_argument('--contract', type=Path, default=DEFAULT)
    args = p.parse_args()
    words = encode(json.loads(args.contract.read_text()), json.loads(args.routes.read_text()), json.loads(args.program.read_text()))
    args.output.write_text(''.join(f'{x:04x}\n' for x in words))


if __name__ == '__main__':
    main()
