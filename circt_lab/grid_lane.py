#!/usr/bin/env python3
"""Lane designs for the grid: assemble a lane-controller program and encode
its fabric (routed bit nets plus tile settings) into a configuration image.

A lane design is JSON:

    {"schema": "spatial.lane-design.v1", "name": ...,
     "nets": [...],                 # grid_route.py workload nets
     "words": {"2": 264, ...},      # tile settings and host words
     "idle_actions": 3,             # action levels while the lane is idle
     "program": "i2c_target.lane",  # assembly, relative to the design file
     "program_base": 768}           # host-bank byte address of the program

Every number the encoder and assembler use (opcodes, event and level
numbers, configuration words, buffer depth) comes from the `lane` section
of the RTL contract, which tools/check_grid_contract.py checks against the
elaborated RTL. Assembly: one instruction per line, optionally after
`label:`; `#` comments; operands are numbers, labels, or contract names
written `event:<name>`, `level:<name>` or `readback:<name>`, combinable
with `|` (for WANY masks, give each event as `bit:event:<name>`).
"""
import argparse
import json
import re
from pathlib import Path

from grid_contract import DEFAULT
import grid_route
from grid_temporal_image import route_words

LABEL = re.compile(r'([A-Za-z_]\w*):(?:\s+|$)')


class Lane:
    def __init__(self, contract):
        self.contract = contract
        self.lane = contract['lane']
        ins = self.lane['instruction']
        self.ops = ins['opcodes']
        self.sys = ins['sys']
        self.events = {n: i for i, n in enumerate(self.lane['events'])}
        self.levels = {n: i + self.lane['level_wait_offset'] for i, n in enumerate(self.lane['levels'])}
        self.readback = {n: i for i, n in enumerate(self.lane['readback'])}
        self.limits = {'WAIT': 15, 'ACT': 15, 'RD': 15, 'SETC': 255, 'WCFG': 255, 'WANY': 255}
        self.no_operand = {'FIRE'}

    def value(self, token, labels):
        total = 0
        for part in token.split('|'):
            bit = part.startswith('bit:')
            name = part[4:] if bit else part
            if name in labels:
                v = labels[name]
            elif name.startswith('event:'):
                v = self.events[name[6:]]
            elif name.startswith('level:'):
                v = self.levels[name[6:]]
            elif name.startswith('readback:'):
                v = self.readback[name[9:]]
            else:
                v = int(name, 0)
            total |= (1 << v) if bit else v
        return total

    def assemble(self, source):
        lines, labels = [], {}
        for number, raw in enumerate(source.splitlines(), 1):
            text = raw.split('#', 1)[0].strip()
            while (match := LABEL.match(text)):
                label = match.group(1)
                if label in labels:
                    raise ValueError(f'line {number}: label {label!r} defined twice')
                labels[label] = len(lines)
                text = text[match.end():].strip()
            if text:
                lines.append((number, text.split()))
        words = []
        for number, parts in lines:
            name = parts[0].upper()
            try:
                if name in self.sys:
                    if len(parts) != 1:
                        raise ValueError(f'{name} takes no operand')
                    words.append(self.ops['SYS'] << 12 | self.sys[name])
                elif name in self.ops and name != 'SYS':
                    if name in self.no_operand:
                        if len(parts) != 1:
                            raise ValueError(f'{name} takes no operand')
                        words.append(self.ops[name] << 12)
                        continue
                    if len(parts) != 2:
                        raise ValueError(f'{name} takes one operand')
                    v = self.value(parts[1], labels)
                    if not 0 <= v <= self.limits.get(name, 0xFFF):
                        raise ValueError(f'{name} operand {v} out of range')
                    if name == 'WCFG' and not self.lane['live_write_words'][0] <= v <= self.lane['live_write_words'][1]:
                        raise ValueError(f'WCFG {v} is outside the lane-owned words')
                    if name == 'WANY' and v == 0:
                        raise ValueError('WANY needs a non-empty event mask')
                    words.append(self.ops[name] << 12 | v)
                else:
                    raise ValueError(f'unknown instruction {parts[0]!r}')
            except (ValueError, KeyError) as e:
                raise ValueError(f'line {number}: {e}') from None
        if not 1 <= len(words) <= self.lane['buffer_instructions']:
            raise ValueError(f'{len(words)} instructions; the lane buffer holds 1..{self.lane["buffer_instructions"]}')
        for (number, parts), word in zip(lines, words):
            if parts[0].upper() in ('J', 'JZ', 'JNZ', 'LOOP') and (word & 0xFFF) >= len(words):
                raise ValueError(f'line {number}: jump past the program end')
        return words

    def program_word(self, base, words):
        fields = self.lane['program_fields']
        span = 2 * len(words)
        if not 0 <= base or base + span > self.contract['resources']['sram0']['bytes']:
            raise ValueError('program does not fit in host bank 0')
        if base >= 1 << (fields['base'][0] + 1):
            raise ValueError('program base out of range')
        return (len(words) - 1) << fields['last'][1] | base

    def encode(self, routes, design, words):
        cfg = self.contract['configuration']
        image = route_words(self.contract, routes, design.get('words', {}), cfg['alternate_context_words'][1] + 1)
        lane = self.lane
        for reserved in (lane['mode_word'], lane['program_word'], lane['idle_actions_word']):
            if str(reserved) in design.get('words', {}):
                raise ValueError(f'word {reserved} is set by the lane encoder')
        idle = design.get('idle_actions', 0)
        if not 0 <= idle <= 15:
            raise ValueError('idle actions are four bits')
        image[lane['mode_word']] = 1 << lane['mode_bit']
        image[lane['idle_actions_word']] = idle << lane['idle_actions_bits'][1]
        image[lane['program_word']] = self.program_word(design['program_base'], words)
        return image


def timer_mode_words(contract, modes):
    """Encode a design's `timer_modes` ({timer: {function, phase,
    resync_limit}}) into the timer mode/phase words, from the contract's
    field layout. Returns {word: value} for words beyond the base image."""
    cfg = contract['configuration']
    fields, functions = cfg['timer_mode_fields'], cfg['timer_functions']
    out = {}
    for name, spec in modes.items():
        resource = contract['resources'].get(name)
        if not resource or resource['kind'] != 'timer':
            raise ValueError(f'timer_modes: {name} is not a timer')
        unknown = set(spec) - {'function', 'phase', 'resync_limit'}
        if unknown:
            raise ValueError(f'timer_modes: {name}: unknown keys {sorted(unknown)}')
        function = spec.get('function', 'hold')
        if function not in functions:
            raise ValueError(f'timer_modes: {name}: unknown function {function}')
        limit, phase = spec.get('resync_limit', 0), spec.get('phase', 0)
        if function != 'resync' and (limit or phase):
            raise ValueError(f'timer_modes: {name}: phase and resync_limit apply to resync only')
        hi, lo = fields['resync_limit']
        if not 0 <= limit < 1 << (hi - lo + 1):
            raise ValueError(f'timer_modes: {name}: resync_limit out of range')
        if function == 'resync' and not 1 <= phase <= 0xffff:
            raise ValueError(f'timer_modes: {name}: resync needs a phase of 1-65535 clocks')
        mode_word, phase_word = resource['mode_words']
        out[mode_word] = functions[function] << fields['function'][1] | limit << lo
        out[phase_word] = phase
    first, last = cfg['timer_mode_words']
    assert all(first <= w <= last for w in out)
    return out


def build(contract, design_path):
    design = json.loads(Path(design_path).read_text())
    if design.get('schema') != 'spatial.lane-design.v1':
        raise ValueError('expected a spatial.lane-design.v1 design')
    lane = Lane(contract)
    words = lane.assemble((Path(design_path).parent / design['program']).read_text())
    routes = grid_route.routes(contract, {'name': design['name'], 'nets': design.get('nets', [])})
    if routes['status'] != 'routed':
        raise ValueError(f'nets did not route: {routes["status"]}')
    image = lane.encode(routes, design, words)
    program = {'schema': 'spatial.lane-program.v1', 'name': design['name'], 'base': design['program_base'],
               'words': words, 'bytes': [b for w in words for b in (w & 255, w >> 8)],
               'routes': routes['route_count'], 'max_hops': routes['max_hops']}
    extended = timer_mode_words(contract, design.get('timer_modes', {}))
    if extended:
        # Words past the base image (timer modes, 208-215): written after it.
        program['extended_words'] = {str(k): v for k, v in sorted(extended.items())}
    return image, program


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('design', type=Path)
    p.add_argument('output', type=Path, help='output prefix: writes <prefix>.mem and <prefix>.program.json')
    p.add_argument('--contract', type=Path, default=DEFAULT)
    args = p.parse_args()
    image, program = build(json.loads(args.contract.read_text()), args.design)
    Path(str(args.output) + '.mem').write_text(''.join(f'{x:04x}\n' for x in image))
    Path(str(args.output) + '.program.json').write_text(json.dumps(program, indent=2) + '\n')


if __name__ == '__main__':
    main()
