#!/usr/bin/env python3
"""Encode routed grid and generic coded-stream tile configuration."""
import argparse
import json
from pathlib import Path
from grid_contract import DEFAULT


def encode(contract, routes, program, base):
    net=contract['bit_network']
    if routes['status']!='routed' or routes['track_count']!=net['tracks_per_direction'] or routes['registered_hops'] or routes['direct_links']:
        raise ValueError('requires current unregistered grid')
    if any(routes['layout'][k]!=v['cell'] for k,v in contract['resources'].items()):
        raise ValueError('route placement does not match RTL')
    words=list(base)+[0]*max(0,124-len(base))
    for i in range(5,41):words[i]=0
    for i in list(range(41,53))+list(range(56,63))+list(range(77,81)):words[i]=0
    for key,value in program['base_words'].items():
        address=int(key)
        if not 0<=address<124 or not 0<=value<=65535:raise ValueError('invalid configuration word')
        words[address]=value
    selections={}
    for route in routes['routes']:
        sink=route['sink']['port']; context=route['sink'].get('context',0)
        if context not in (0,1):raise ValueError('unknown routing context')
        key=(context,sink)
        if key in selections:raise ValueError('duplicate sink')
        selections[key]=route['sink_select']
        for hop in route['hops']:
            addr=net['switch_words']['first']+hop['from']*net['switch_words']['words_per_cell']+hop['direction']
            shift=route['track']*net['switch_words']['selector_bits']
            old=(words[addr]>>shift)&7
            if old and old!=hop['selector']:raise ValueError('switch conflict')
            words[addr]=(words[addr]&~(7<<shift))|(hop['selector']<<shift)
    for (context,sink),select in selections.items():
        if context:continue
        if 77<=sink<=80:addr=sink
        elif 0<=sink<len(net['sink_cells']):
            addr=net['sink_word_first']+sink if sink<net['sink_word_split'] else net['sink_word_second']+sink-net['sink_word_split']
        else:raise ValueError('unknown sink')
        words[addr]=select
    temporal=program.get('temporal')
    if temporal:
        rows=temporal['rows']
        if len(rows)!=16 or any(not isinstance(x,int) or not 0<=x<=65535 for x in rows):raise ValueError('invalid temporal program')
        overrides=[]
        for (context,sink),select in selections.items():
            if not context:continue
            if not 0<=sink<len(net['sink_cells']):raise ValueError('alternate CRC selector unsupported')
            addr=net['sink_word_first']+sink if sink<net['sink_word_split'] else net['sink_word_second']+sink-net['sink_word_split']
            if select!=words[addr]:overrides.append(0x400|(select<<5)|sink)
        if len(overrides)>contract['temporal']['alternate_context_slots']:raise ValueError('too many context overrides')
        for i,word in enumerate(overrides):words[112+i]=word
        words[91]=1;words[92]=temporal['entry']|(temporal['idle_actions']<<4)
        words[96:112]=rows
    elif any(context for context,_ in selections):raise ValueError('alternate routes require temporal program')
    return words


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('routes',type=Path);p.add_argument('program',type=Path);p.add_argument('base',type=Path);p.add_argument('output',type=Path)
    p.add_argument('--contract',type=Path,default=DEFAULT);a=p.parse_args()
    words=encode(json.loads(a.contract.read_text()),json.loads(a.routes.read_text()),json.loads(a.program.read_text()),[int(s,16) for s in a.base.read_text().splitlines()])
    a.output.write_text(''.join(f'{x:04x}\n' for x in words))
if __name__=='__main__':main()
