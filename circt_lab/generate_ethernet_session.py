#!/usr/bin/env python3
"""Rewrite the Ethernet source's NLP_TICK (a source parameter) for a fixture.

A shorter tick keeps the same session program and pulse/frame structure but
lets gate-level simulation cover three link pulses and a frame quickly.
"""
import argparse
import re
from pathlib import Path

p = argparse.ArgumentParser(description=__doc__)
p.add_argument('source', type=Path); p.add_argument('output', type=Path)
p.add_argument('--nlp-tick', type=int, required=True)
a = p.parse_args()
if not 1 <= a.nlp_tick <= 65535:
    raise ValueError('NLP_TICK must fit a 16-bit timer')
text, n = re.subn(r'const NLP_TICK = \d+ cycles', f'const NLP_TICK = {a.nlp_tick} cycles', a.source.read_text(), count=1)
if n != 1:
    raise ValueError('missing NLP_TICK declaration')
a.output.write_text(text)
