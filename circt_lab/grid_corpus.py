#!/usr/bin/env python3
"""Extract named, direct bit-connection slices from six protocol programs.

Mappings are explicit hypotheses, not complete protocol compilation. The Rust
frontend and CIRCT evidence exporter must confirm every included source edge.
"""
import argparse
import json
from pathlib import Path
import subprocess
import sys

HERE = Path(__file__).resolve().parent

# source signal -> (physical resource, local output port)
SOURCES = {
    'i2c': {'sda_bits.out': ('shifter0', 2), 'sda_bits.done': ('shifter0', 1),
            'pattern.busy': ('shifter1', 0), 'pattern.done': ('shifter1', 1),
            'cond_clock.tick': ('timer1', 0)},
    'jtag': {'clock.out': ('timer0', 3), 'clock.fall': ('timer0', 2), 'clock.rise': ('timer0', 1),
             'data.busy': ('shifter0', 0), 'data.done': ('shifter0', 1), 'data.out': ('shifter0', 2),
             'mode.out': ('shifter1', 2), 'tdo': ('input_io', 0), 'go': ('sram0', 0)},
    'usb': {'bits.busy': ('shifter0', 0), 'bits.out': ('shifter0', 2)},
    'ethernet_tx': {'half_bit.fall': ('timer0', 2), 'bits.out': ('shifter0', 2)},
}
# (source consumer, field) -> (physical resource, sink selector number)
SINKS = {
    'i2c': {('sda','pin.drive'): ('output_io', 8), ('byte_clock','timer.stop'): ('timer0', 1),
            ('cond_clock','timer.start'): ('timer1', 12), ('cond_clock','timer.stop'): ('timer1', 13),
            ('pattern','shifter.shift'): ('shifter1', 16)},
    'jtag': {('tck','pin.drive'): ('output_io', 7), ('tms','pin.drive'): ('output_io', 8),
             ('tdi','pin.drive'): ('output_io', 9), ('clock','timer.start'): ('timer0', 0),
             ('clock','timer.stop'): ('timer0', 1), ('data','shifter.start'): ('shifter0', 3),
             ('data','shifter.shift'): ('shifter0', 4), ('data','shifter.sample_at'): ('shifter0', 5),
             ('data','shifter.sample_data'): ('shifter0', 6), ('mode','shifter.start'): ('shifter1', 15),
             ('mode','shifter.shift'): ('shifter1', 16)},
    'usb': {('bit','timer.start'): ('timer0', 0), ('check','crc.data'): ('crc', 80)},
    'ethernet_tx': {('bits','shifter.start'): ('shifter0', 3), ('bits','shifter.shift'): ('shifter0', 4),
                    ('fcs','crc.absorb'): ('crc', 77), ('fcs','crc.emit'): ('crc', 78),
                    ('fcs','crc.data'): ('crc', 80)},
}


def derive(program, evidence):
    sources, targets = SOURCES[program], SINKS[program]
    nets, used = {}, set()
    for edge in evidence['connections']:
        key = edge['consumer'], edge['port']
        if key not in targets or edge['context'] is not None:
            continue
        refs = edge['references']
        if edge['check']['status'] != 'valid' or edge['expression']['kind'] not in ('name', 'field') or len(refs) != 1:
            raise ValueError(f'{program}: selected connection {key} is no longer a direct valid source')
        ref = refs[0]
        name = ref['name'] + ('.' + ref['output'] if ref.get('output') else '')
        if name not in sources:
            raise ValueError(f'{program}: unknown source {name} for {key}')
        if key in used:
            raise ValueError(f'{program}: duplicated selected connection {key}')
        used.add(key)
        origin, local = sources[name]
        dest, port = targets[key]
        net = nets.setdefault(name, {'name': name, 'source': {'resource': origin, 'port': local}, 'sinks': []})
        net['sinks'].append({'resource': dest, 'port': port})
    if used != set(targets):
        raise ValueError(f'{program}: missing selected connections: {set(targets) - used}')
    return {'name': program + '-direct-bit-slice', 'source_validated': True,
            'full_source_program_compiled': False, 'nets': list(nets.values()),
            'selected_connections': len(used), 'all_source_connections': len(evidence['connections']),
            'source_resource_counts': evidence['source_resource_counts'],
            'limitations': ['Explicit placement hypothesis; omitted control, logic expressions, phases, and memory.',
                            'Full protocol support cannot be inferred from this slice.']}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('program', choices=sorted(SOURCES))
    parser.add_argument('--frontend', required=True)
    parser.add_argument('--mapper', required=True)
    parser.add_argument('--source', type=Path)
    args = parser.parse_args()
    source = args.source or HERE.parent / 'examples' / (args.program + '.spt')
    raw = subprocess.run([sys.executable, str(HERE / 'investigate_workloads.py'), '--frontend', args.frontend,
                          '--mapper', args.mapper, str(source)], check=True, capture_output=True, text=True)
    print(json.dumps(derive(args.program, json.loads(raw.stdout)['programs'][0]), indent=2))


if __name__ == '__main__':
    main()
