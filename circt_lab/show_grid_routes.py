#!/usr/bin/env python3
"""Show a concrete routed grid image as an ASCII resource map."""
import argparse
from pathlib import Path
import json
from grid_contract import DEFAULT


def render(contract, report, program=None):
    if report['status']!='routed':
        raise ValueError('report must contain successful routes')
    layout=report['layout']; routes=report['routes'];resources=contract['resources']
    used=set()
    for route in routes:
        used.add(route['source']['resource']);used.add(route['sink']['resource'])
    if program and program.get('payload_bytes'):
        base=program.get('payload_base',100);end=base+len(program['payload_bytes'])
        for i in range(contract['memory']['bank_count']):
            size=resources[f'sram{i}']['bytes']
            if base<(i+1)*size and end>i*size:used.add(f'sram{i}')
    cells=[]
    for cell in contract['grid']['cells']:
        names=[name for name in resources if layout[name]==cell['id']]
        cells.append('['+'+'.join(name+('*' if name in used else '') for name in names)+']')
    width=max(len(x) for x in cells)
    lines=['* = source or destination of a routed bit net; SRAM* also indicates payload storage.']
    cols=contract['grid']['columns']
    for start in range(0,len(cells),cols):
        lines.append(' -- '.join(f'{cell:<{width}}' for cell in cells[start:start+cols]).rstrip())
        if start+cols<len(cells):lines.append('     |'.ljust(width+4)*cols)
    lines.append('\nRouted bit connections:')
    for path in routes:
        source=path['source'];sink=path['sink'];ctx=sink.get('context',0)
        hops=' -> '.join(str(x['from']) for x in path['hops'])
        if path['hops']:hops+=' -> '+str(path['hops'][-1]['to'])
        else:hops=str(layout[source['resource']])
        lines.append(f"  {path['net']}: {source['resource']}[{source['port']}] -> {sink['resource']}[{sink['port']}] context {ctx}, track {path['track']}, cells {hops}")
    if program and 'payload_bytes' in program:
        base=program.get('payload_base',100);length=len(program['payload_bytes'])
        lines.append(f'\nPayload: {length} bytes starting at logical SRAM address {base}.')
    if program and program.get('temporal'):
        lines.append('Sequencer actions are injected at the SRAM0 cell; SRAM does not execute instructions.')
    lines.append('Fixed placement and routed bit paths only; this is not physical utilization or a complete schedule.')
    return '\n'.join(lines)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('routes',type=Path);parser.add_argument('--program',type=Path);parser.add_argument('--contract',type=Path,default=DEFAULT)
    a=parser.parse_args()
    print(render(json.loads(a.contract.read_text()),json.loads(a.routes.read_text()),json.loads(a.program.read_text()) if a.program else None))
if __name__=='__main__':main()
