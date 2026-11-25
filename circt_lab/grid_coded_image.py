#!/usr/bin/env python3
"""Encode routed grid and generic coded-stream tile configuration."""
import argparse
import json
from pathlib import Path
from grid_contract import DEFAULT
import grid_pager


def switch_address(net, cell, direction):
    words=net['switch_words']
    first_ext=words.get('extension_first_cell')
    if first_ext is not None and cell>=first_ext:
        return words['extension_first']+(cell-first_ext)*words['words_per_cell']+direction
    return words['first']+cell*words['words_per_cell']+direction


def sink_address(net, sink):
    if 'sink_words' in net:
        return net['sink_words'][sink]
    return net['sink_word_first']+sink if sink<net['sink_word_split'] else net['sink_word_second']+sink-net['sink_word_split']


def temporal_rows(temporal):
    """Flat rows [halt, no, yes, event, action] from either program form."""
    if 'program' in temporal:
        rows=temporal['program']
        if not rows or any(not isinstance(r,list) or len(r)!=5 or not all(isinstance(x,int) for x in r) for r in rows):
            raise ValueError('invalid temporal program')
        return rows
    rows=temporal['rows']
    if len(rows)!=16 or any(not isinstance(x,int) or not 0<=x<=65535 for x in rows):raise ValueError('invalid temporal program')
    return [[x>>15,(x>>11)&15,(x>>7)&15,(x>>4)&7,x&15] for x in rows]


def static_bases(program):
    """SRAM addresses the program states as constants (streams and preloads)."""
    found=[]
    for key in ('89','90'):
        if isinstance(program['base_words'].get(key),int):found.append(program['base_words'][key])
    for fn in program.get('interface',{}).get('functions',{}).values():
        for key in ('89','90'):
            if isinstance(fn.get('words',{}).get(key),int):found.append(fn['words'][key])
        for region in fn.get('sram',[]):
            if isinstance(region.get('base'),int):found.append(region['base'])
    return found


def encode(contract, routes, program, base, page_rows=grid_pager.PAGE_ROWS, sram=None):
    """Return the configuration words. A program longer than one page is
    split into SRAM pages; their bytes are appended to `sram` as
    {'base', 'bytes'} regions to write after arming."""
    net=contract['bit_network']
    if routes['status']!='routed' or routes['track_count']!=net['tracks_per_direction'] or routes['registered_hops'] or routes['direct_links']:
        raise ValueError('requires current unregistered grid')
    if any(routes['layout'][k]!=v['cell'] for k,v in contract['resources'].items()):
        raise ValueError('route placement does not match RTL')
    # Images that use the extension rows (cells 9-14, words 128-184) are
    # longer; images for the original nine cells keep their 124 words.
    first_ext=net['switch_words'].get('extension_first_cell',len(contract['grid']['cells']))
    extended=(any(int(k)>=128 for k in program['base_words']) or
              any(h['from']>=first_ext or h['to']>=first_ext for r in routes['routes'] for h in r['hops']) or
              any(r['sink']['port']>=contract['bit_network'].get('context_overridable_sinks',19) and r['sink']['resource']!='crc' for r in routes['routes']))
    size=contract['configuration']['extension_words'][1]+1 if extended else 124
    words=list(base[:124])+[0]*max(0,size-min(len(base),124))
    for i in range(5,41):words[i]=0
    for i in list(range(41,53))+list(range(56,63))+list(range(77,81))+list(range(128,size)):words[i]=0
    for key,value in program['base_words'].items():
        address=int(key)
        if not 0<=address<size or not 0<=value<=65535:raise ValueError('invalid configuration word')
        words[address]=value
    selections={}
    for route in routes['routes']:
        sink=route['sink']['port']; context=route['sink'].get('context',0)
        if context not in (0,1):raise ValueError('unknown routing context')
        key=(context,sink)
        if key in selections:raise ValueError('duplicate sink')
        selections[key]=route['sink_select']
        for hop in route['hops']:
            addr=switch_address(net,hop['from'],hop['direction'])
            shift=route['track']*net['switch_words']['selector_bits']
            old=(words[addr]>>shift)&7
            if old and old!=hop['selector']:raise ValueError('switch conflict')
            words[addr]=(words[addr]&~(7<<shift))|(hop['selector']<<shift)
    for (context,sink),select in selections.items():
        if context:continue
        if 77<=sink<=80:addr=sink
        elif 0<=sink<len(net['sink_cells']):addr=sink_address(net,sink)
        else:raise ValueError('unknown sink')
        words[addr]=select
    temporal=program.get('temporal')
    if temporal:
        flat=temporal_rows(temporal)
        pages=grid_pager.paginate(flat,temporal['entry'],temporal.get('stretchable_rows',[]),page_rows)
        rows=pages[0] if pages else [grid_pager.pack(r) for r in flat]+[0x8000]*(16-len(flat))
        overrides=[]
        for (context,sink),select in selections.items():
            if not context:continue
            if not 0<=sink<net.get('context_overridable_sinks',len(net['sink_cells'])):raise ValueError('sink has no alternate-context override')
            addr=sink_address(net,sink)
            if select!=words[addr]:overrides.append(0x400|(select<<5)|sink)
        if len(overrides)>contract['temporal']['alternate_context_slots']:raise ValueError('too many context overrides')
        for i,word in enumerate(overrides):words[112+i]=word
        words[91]=1;words[92]=temporal['entry']|(temporal['idle_actions']<<4)
        words[96:112]=rows
        if pages:
            if size<=94:raise ValueError('paging needs the extended image (word 94)')
            if (words[88]&1):raise ValueError('pages are placed in bank 0; route the host to bank 0')
            start=grid_pager.page_base(len(pages))
            if any(b>=start for b in static_bases(program)):raise ValueError(f'SRAM above {start} is reserved for sequencer pages')
            words[94]=0x8000|start
            if sram is None:raise ValueError('paged program needs an SRAM output')
            for offset,data in grid_pager.page_bytes(pages,words[112:117]):
                sram.append({'base':start+offset,'bytes':data})
    elif any(context for context,_ in selections):raise ValueError('alternate routes require temporal program')
    return words


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('routes',type=Path);p.add_argument('program',type=Path);p.add_argument('base',type=Path);p.add_argument('output',type=Path)
    p.add_argument('--contract',type=Path,default=DEFAULT)
    p.add_argument('--page-rows',type=int,default=grid_pager.PAGE_ROWS,help='rows per page (below 16 only to exercise paging)')
    a=p.parse_args()
    program=json.loads(a.program.read_text());sram=[]
    words=encode(json.loads(a.contract.read_text()),json.loads(a.routes.read_text()),program,[int(s,16) for s in a.base.read_text().splitlines()],a.page_rows,sram)
    a.output.write_text(''.join(f'{x:04x}\n' for x in words))
    if sram:
        # Static SRAM contents for the host to write once after arming.
        program.setdefault('interface',{})['setup_sram']=sram
        a.program.write_text(json.dumps(program,indent=2)+'\n')
if __name__=='__main__':main()
