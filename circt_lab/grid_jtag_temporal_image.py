#!/usr/bin/env python3
"""Encode CIRCT JTAG temporal rows on the source-checked grid component."""
import argparse
import json
from pathlib import Path

p=argparse.ArgumentParser(description=__doc__)
p.add_argument('component',type=Path);p.add_argument('program',type=Path);p.add_argument('output',type=Path)
a=p.parse_args()
words=[int(s,16) for s in a.component.read_text().splitlines()]
program=json.loads(a.program.read_text());temporal=program['temporal'];rows=temporal['rows']
if len(words)!=66 or len(rows)!=16 or any(not isinstance(x,int) or not 0<=x<=65535 for x in rows):
    raise ValueError('invalid component or temporal table')
payload=program['payload_bytes'];rx=program['rx_capacity']
if len(payload)!=2*rx or not 0<len(payload)<=384:
    raise ValueError('invalid JTAG stream bounds')
# In legacy mode the SRAM-cell local port 0 is the frame pulse; in temporal
# mode the same pulse is local port 3. Retarget its outgoing switch tracks.
for address in range(29,33):
    for track in range(4):
        shift=3*track
        if (words[address]>>shift)&7==1:
            words[address]=(words[address]&~(7<<shift))|(4<<shift)
words.extend([0]*(117-len(words)))
words[66]=len(payload);words[67]=rx;words[68]=0
words[88]=0;words[89]=100;words[90]=200
words[91]=1;words[92]=temporal['entry']|(temporal['idle_actions']<<4)
words[96:112]=rows
# All context routes are shared by the two temporal phases.
a.output.write_text(''.join(f'{x:04x}\n' for x in words))
