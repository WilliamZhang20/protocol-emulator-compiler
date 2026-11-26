#!/usr/bin/env python3
"""R6: the channel-width sweep over the corpus workloads (fixtures from
grid_corpus.py / grid_workload.py; regenerate with the CIRCT mapper)."""
import json
from pathlib import Path

from grid_chanwidth import netlist, fits, min_width
from grid_rrg import load

HERE = Path(__file__).resolve().parent
contract = load()
expect = {'spi': 2, 'uart': 2, 'i2c': 2, 'jtag': 2, 'usb': 1, 'ethernet_tx': 1}
loaded = {}
for name, want in expect.items():
    w = json.loads((HERE / 'workloads' / f'{name}.json').read_text())
    blocks, nets = netlist(contract, w, prefix=name + '.')
    assert sum(len(n['sinks']) for n in nets) == sum(len(n['sinks']) for n in w['nets'])
    got, result = min_width(contract, blocks, nets)
    assert got == want, (name, got, want)
    loaded[name] = (blocks, nets)
# Concurrency is bounded by instances today: SPI with USB fits, SPI with UART does not.
both = {**loaded['spi'][0], **loaded['usb'][0]}
assert fits(contract, both)
assert min_width(contract, both, loaded['spi'][1] + loaded['usb'][1])[0] == 2
assert not fits(contract, {**loaded['spi'][0], **loaded['uart'][0]})
# A port placement cannot reach is refused, not silently dropped.
bad = {'name': 'bad', 'nets': [{'name': 'n', 'source': {'resource': 'timer0', 'port': 9},
                                'sinks': [{'resource': 'output_io', 'port': 7}]}]}
try:
    netlist(contract, bad)
except ValueError as e:
    assert 'not a placeable port' in str(e)
else:
    raise AssertionError('accepted a missing output port')
report = json.loads((HERE / 'reports' / 'channel_width.json').read_text())
assert {tuple(r['workloads']): r['min_tracks'] for r in report['rows']}
print(f'PASS: minimum channel width of the six corpus workloads {expect} (contract has '
      f'{contract["bit_network"]["tracks_per_direction"]}); SPI+USB at 2; instance limits refuse SPI+UART')
