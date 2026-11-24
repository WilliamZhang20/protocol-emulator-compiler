#!/usr/bin/env python3
"""Convert a source-validated SPI/UART image report to explicit bit nets."""
import argparse
import json
from pathlib import Path

from grid_contract import DEFAULT

SINKS = (['timer0'] * 3 + ['shifter0'] * 4 + ['output_io'] * 3 + ['lut'] * 2 +
         ['timer1'] * 3 + ['shifter1'] * 4)
SOURCE = {0: 'input_io', 2: 'timer0', 3: 'shifter0', 4: 'lut',
          5: 'timer1', 6: 'sram0', 7: 'shifter1', 8: 'crc'}


def from_report(contract, report):
    if report.get('target') not in ('grid-3x3-spi-experiment', 'grid-3x3-uart-experiment') or not report.get('source_validated'):
        raise ValueError('expected source-validated grid SPI/UART component report')
    nets = []
    for route in report['routing']:
        resource = SOURCE[route['source_cell']]
        if contract['resources'][resource]['cell'] != route['source_cell']:
            raise ValueError('report and contract source placement disagree')
        sinks = []
        for path in route['paths']:
            sink = path['sink']
            resource_name = SINKS[sink]
            if contract['resources'][resource_name]['cell'] != path['cell'] or contract['bit_network']['sink_cells'][sink] != path['cell']:
                raise ValueError('report and contract sink placement disagree')
            sinks.append({'resource': resource_name, 'port': sink})
        nets.append({'name': route['signal'], 'source': {'resource': resource, 'port': route['source_port']}, 'sinks': sinks})
    return {'name': report['target'], 'source_validated': True,
            'full_source_program_compiled': False, 'nets': nets}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('report', type=Path)
    parser.add_argument('--contract', type=Path, default=DEFAULT)
    args = parser.parse_args()
    print(json.dumps(from_report(json.loads(args.contract.read_text()), json.loads(args.report.read_text())), indent=2))


if __name__ == '__main__':
    main()
