#!/usr/bin/env python3
"""Encode current-grid experiment routes into an existing SPI/UART image."""
import argparse
import json
from collections import defaultdict
from pathlib import Path

from grid_contract import DEFAULT


def encode(contract, result, original, words):
    network = contract['bit_network']
    if result['status'] != 'routed' or result['track_count'] != network['tracks_per_direction'] or result['registered_hops'] or result['direct_links']:
        raise ValueError('image encoding requires the routed, unregistered current track topology')
    if any(result['layout'][name] != resource['cell'] for name, resource in contract['resources'].items()):
        raise ValueError('image encoding requires the current RTL placement')
    words = list(words)
    for i in range(5, 41):
        words[i] = 0
    for i in list(range(41, 53)) + list(range(56, 63)):
        words[i] = 0
    routes = defaultdict(list)
    for path in result['routes']:
        for hop in path['hops']:
            addr = network['switch_words']['first'] + hop['from'] * network['switch_words']['words_per_cell'] + hop['direction']
            shift = path['track'] * network['switch_words']['selector_bits']
            words[addr] = (words[addr] & ~(7 << shift)) | (hop['selector'] << shift)
        sink = path['sink']['port']
        split = network['sink_word_split']
        addr = network['sink_word_first'] + sink if sink < split else network['sink_word_second'] + sink - split
        words[addr] = path['sink_select']
        routes[path['net']].append(path)
    output = dict(original)
    output['routing'] = []
    for net in original['routing']:
        paths = routes[net['signal']]
        tracks = {x['track'] for x in paths}
        if len(tracks) != 1:
            raise ValueError('existing independent image checker requires one track per net')
        output['routing'].append({**net, 'source_cell': result['layout'][paths[0]['source']['resource']],
                                  'track': next(iter(tracks)),
                                  'paths': [{'cell': result['layout'][x['sink']['resource']],
                                             'sink': x['sink']['port'], 'hops': x['hops']} for x in paths]})
    return words, output


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('route', type=Path)
    p.add_argument('report', type=Path)
    p.add_argument('image', type=Path)
    p.add_argument('output_image', type=Path)
    p.add_argument('output_report', type=Path)
    p.add_argument('--contract', type=Path, default=DEFAULT)
    args = p.parse_args()
    words, report = encode(json.loads(args.contract.read_text()), json.loads(args.route.read_text()),
                           json.loads(args.report.read_text()),
                           [int(x, 16) for x in args.image.read_text().splitlines()])
    args.output_image.write_text(''.join(f'{x:04x}\n' for x in words))
    args.output_report.write_text(json.dumps(report, indent=2) + '\n')


if __name__ == '__main__':
    main()
