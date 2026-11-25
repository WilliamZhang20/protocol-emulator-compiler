#!/usr/bin/env python3
"""Generate a deterministic Ethernet source fixture with a chosen payload length."""
import argparse
from pathlib import Path
import re

p=argparse.ArgumentParser(description=__doc__)
p.add_argument('source',type=Path);p.add_argument('output',type=Path)
p.add_argument('--payload-bytes',type=int,default=1500)
a=p.parse_args()
if not 46<=a.payload_bytes<=1501:raise ValueError('fixture payload must be 46..1501 bytes')
frame=bytes.fromhex('ffffffffffff02123456789a0800')+bytes((i*73+19)&255 for i in range(a.payload_bytes))
wire=bytes([0x55]*7+[0xd5])+frame
source=a.source.read_text()
for name,data in [('FRAME',frame),('WIRE_FRAME',wire)]:
    replacement='const '+name+' = ['+', '.join(f'0x{x:02X}' for x in data)+']'
    source,n=re.subn(r'const '+name+r' = \[[^\]]*\]',lambda _:replacement,source,count=1)
    if n!=1:raise ValueError(f'missing {name} declaration')
a.output.write_text(source)
