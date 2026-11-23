#!/usr/bin/env python3
"""Cross-check the documented slice inventory against its checked-out RTL."""
import argparse
import json
from pathlib import Path
import re


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('rtl_root', type=Path)
    args = p.parse_args()
    contract = json.loads((Path(__file__).parent / 'candidates/current-slice-rtl-contract.json').read_text())
    rtl = (args.rtl_root / contract['rtl']).read_text()
    rtl = re.sub(r'//[^\n]*', '', rtl)
    for module, count in contract['resources'].items():
        pattern = r'\b' + re.escape(module) + r'\s*(?:#\s*\([^;]*?\)\s*)?\w+\s*\('
        assert len(re.findall(pattern, rtl, flags=re.S)) == count, module
    for entry in contract['routes']:
        pattern = (r'\bproto_route\s+' + re.escape(entry['instance']) +
                   r'\s*\(\s*clk\s*,\s*live\s*,\s*routes\[(\d+)\s*:\s*(\d+)\]\s*,\s*'
                   r'\{([^}]*)\}\s*,\s*(\w+)\s*\)')
        match = re.search(pattern, rtl)
        assert match, entry['instance']
        assert [int(match[1]), int(match[2])] == entry['select_bits']
        assert [s.strip() for s in match[3].split(',')][::-1] == entry['choices']
        assert match[4] == entry['destination']
    for key, module in [('timer', 'proto_timer'), ('shifter', 'proto_shift')]:
        item = contract[key]
        match = re.search(r'\b' + module + r'\s+' + item['instance'] + r'\s*\(([^;]*)\)\s*;', rtl, re.S)
        assert match, item['instance']
        assert [s.strip() for s in match[1].split(',')] == item['inputs'] + item['outputs']
    for item in contract['other_ports']:
        match = re.search(r'\b' + item['module'] + r'\s+' + item['instance'] + r'\s*\(([^;]*)\)\s*;', rtl, re.S)
        assert match, item['instance']
        assert [s.strip() for s in match[1].split(',')] == item['ports']
    modules = (args.rtl_root / 'src/spi_tiles.v').read_text()
    assert 'assign serial_out = tx_shift[15];' in modules
    assert "tx_shift <= {tx_shift[14:0], 1'b0}" in modules
    assert 'if (count == half_period - 1\'b1)' in modules
    assert 'else out <= sources[select];' in modules
    memory = (args.rtl_root / 'src/memory_tiles.v').read_text()
    assert len(re.findall(r'\bexperiment_memory_bank\s*#\s*\(\.USE_MACRO\(USE_MACRO\)\)\s*bank[01]\s*\(', memory)) == contract['memory']['banks']
    assert 'reg [7:0] words [0:1023];' in memory
    assert contract['memory']['bank_depth_bytes'] == 1024 and contract['memory']['read_write_ports_per_bank'] == 1
    candidate = (Path(__file__).parent / 'candidates/memory-spi-graph.mlir').read_text()
    for module, kind in [('proto_timer', 'timer'), ('proto_shift', 'shifter')]:
        assert len(re.findall(r'\bid = "[^"]+", resource = "' + kind + r'"', candidate)) == contract['resources'][module]
    assert len(contract['routes']) == 8 and len({x['destination'] for x in contract['routes']}) == 8
    print('PASS: current slice inventory matches RTL resources, ports and eight selector tables')


if __name__ == '__main__':
    main()
