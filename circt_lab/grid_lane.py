#!/usr/bin/env python3
"""Lane designs for the lane core: assemble a lane-controller program and
encode the core's local wiring and tile settings into its page-1 words.

A lane design (schema v2) names the core's tiles and lines; there is no
routing, every tile input selects one of the core's local lines:

    {"schema": "spatial.lane-design.v2", "name": ...,
     "connect": {"timer0.start": "fire",        # selector <- line
                 "coder.shift": "timer0.tick",
                 "out0.value": "coder.serial", "out0.enable": "high"},
     "timers": {"timer0": {"first": 9, "period": 20,
                           "function": "resync", "phase": 7, "resync_limit": 8}},
     "coder": {"frame": {"length": 8, "pre": 1, "post": 1, "post_value": 1,
                         "parity": "even", "idle": 1},
               "io": {"auto_pull": true}, "coder": {...}, "burst": {...}},
     "crc": {"polynomial": 0x8005, "residue": 0, "shape": {...}},
     "host_words": {"68": 3},       # page-0 host words (pad feedback)
     "idle_actions": 0,
     "program": "uart_rx.lane",     # assembly, relative to the design file
     "program_base": 768}           # host-bank byte address of the program

Every number comes from the RTL contract (`lane` and `lane_core`
sections), which tools/check_grid_contract.py checks against the RTL and the
core's validity proof. The encoder enforces the rules the core checks before
it arms (defined lines only, selector kinds, coder and CRC rules, timers set
in pairs), so a design that builds is a configuration the core accepts.

Assembly: one instruction per line, optionally after `label:`; `#`
comments; operands are numbers, labels, or names:
  event:<line>       a controller event slot wired to that line (allocated
                     from `connect` "eventN" entries or automatically)
  level:<line>       a level slot (WAIT gives level slots 8-15)
  bit:event:<line>   the slot's bit, for WANY masks (combine with `|`)
  readback:<name>    a readback slot (lane_core.readback)
  word:<name>        a live-writable core word for WCFG (timer0.first, ...)
"""
import argparse
import json
import re
from pathlib import Path

from grid_contract import DEFAULT

LABEL = re.compile(r'([A-Za-z_]\w*):(?:\s+|$)')
NAME_OPERAND = re.compile(r'(?:bit:)?(event|level):([\w.]+)')


class Lane:
    def __init__(self, contract):
        self.contract = contract
        self.lane = contract['lane']
        self.core = contract['lane_core']
        ins = self.lane['instruction']
        self.ops = ins['opcodes']
        self.sys = ins['sys']
        self.lines = {name: index for index, name, _ in self.core['lines']}
        self.readback = {n: i for i, n in enumerate(self.core['readback'])}
        timers = self.core['word_layout']
        self.live_words = {}
        for t in ('timer0', 'timer1'):
            self.live_words[f'{t}.first'] = timers[t][0]
            self.live_words[f'{t}.period'] = timers[t][0] + 1
        assert sorted(self.live_words.values()) == self.lane['live_write_accepted']
        self.limits = {'WAIT': 15, 'ACT': 15, 'RD': 15, 'SETC': 255, 'WCFG': 255, 'WANY': 255}
        self.no_operand = {'FIRE'}
        self.events = {}   # line name -> slot, set by allocate()
        self.levels = {}

    def allocate(self, source, connect):
        """Event and level slots: fixed by `connect` ("event3": line), the
        rest given in order of first use in the program."""
        fixed = {'event': {}, 'level': {}}
        for selector, line in connect.items():
            m = re.fullmatch(r'(event|level)(\d+)', selector)
            if m:
                kind, slot = m.group(1), int(m.group(2))
                if line in fixed[kind] and fixed[kind][line] != slot:
                    raise ValueError(f'{line} is wired to two {kind} slots')
                fixed[kind].setdefault(line, slot)
        used = {'event': [], 'level': []}
        for raw in source.splitlines():
            for kind, line in NAME_OPERAND.findall(raw.split('#', 1)[0]):
                if line not in used[kind]:
                    used[kind].append(line)
        for kind, count in (('event', self.lane['events']), ('level', self.lane['levels'])):
            slots = dict(fixed[kind])
            free = [s for s in range(count) if s not in slots.values()]
            for line in used[kind]:
                if line not in self.lines:
                    raise ValueError(f'{kind}:{line}: no such line')
                if line not in slots:
                    if not free:
                        raise ValueError(f'more than {count} {kind} lines')
                    slots[line] = free.pop(0)
            setattr(self, kind + 's', slots)
        return {f'event{s}': line for line, s in self.events.items()} | \
               {f'level{s}': line for line, s in self.levels.items()}

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
                v = self.levels[name[6:]] + self.lane['level_wait_offset']
            elif name.startswith('readback:'):
                v = self.readback[name[9:]]
            elif name.startswith('word:'):
                if name[5:] not in self.live_words:
                    raise ValueError(f'{name} is not a live-writable word')
                v = self.live_words[name[5:]]
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
                    if name == 'WCFG' and v not in self.lane['live_write_accepted']:
                        raise ValueError(f'WCFG {v} is outside the live-writable core words')
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


def put(words, word, hi, lo, value, what):
    width = hi - lo + 1
    if not 0 <= value < 1 << width:
        raise ValueError(f'{what}: {value} does not fit {width} bits')
    words[word] = (words[word] & ~(((1 << width) - 1) << lo)) | value << lo


class CoreEncoder:
    """The 38 page-1 words of one lane core from a v2 design."""

    def __init__(self, contract):
        self.contract = contract
        self.core = contract['lane_core']
        self.lines = {name: index for index, name, _ in self.core['lines']}
        self.selectors = {name: (word, bits, kind) for name, word, bits, kind in self.core['selectors']}
        self.pulses = self.core['pulse_lines']
        self.levels = self.core['level_lines']

    def line_value(self, selector, line):
        if selector not in self.selectors:
            raise ValueError(f'connect: no selector {selector!r}')
        if line not in self.lines:
            raise ValueError(f'connect: {selector} <- {line!r}: no such line')
        index = self.lines[line]
        _, _, kind = self.selectors[selector]
        if kind == 'level':
            if not self.levels[0] <= index <= self.levels[1]:
                raise ValueError(f'connect: {selector} takes a level line, {line} is a pulse')
            return index - self.levels[0]
        if kind == 'pulse5':
            if not self.pulses[0] <= index <= self.pulses[1]:
                raise ValueError(f'connect: {selector} takes a pulse line, {line} is a level')
        return index

    def fields(self, words, register, spec, extra=None):
        reg = self.core['registers'][register]
        for name, value in spec.items():
            if extra and name in extra:
                continue
            if name not in reg['fields']:
                raise ValueError(f'{register}: unknown field {name!r}')
            hi, lo = reg['fields'][name]
            put(words, reg['word'], hi, lo, int(value), f'{register}.{name}')

    def encode(self, design, connect):
        core = self.core
        layout = core['word_layout']
        words = [0] * core['words']
        for selector, line in connect.items():
            value = self.line_value(selector, line)   # raises for an unknown selector or line
            word, (hi, lo), _ = self.selectors[selector]
            put(words, word, hi, lo, value, selector)
            if selector in core['gate_enables']:
                gw, gb = core['gate_enables'][selector]
                words[gw] |= 1 << gb
        # Timers: first, period, mode, phase.
        cfg = self.contract['configuration']
        mode_fields, functions = cfg['timer_mode_fields'], cfg['timer_functions']
        for name, spec in design.get('timers', {}).items():
            if name not in ('timer0', 'timer1'):
                raise ValueError(f'timers: no timer {name!r} in the lane core')
            unknown = set(spec) - {'first', 'period', 'function', 'phase', 'resync_limit'}
            if unknown:
                raise ValueError(f'timers: {name}: unknown keys {sorted(unknown)}')
            base = layout[name][0]
            first, period = spec.get('first', 0), spec.get('period', 0)
            if (first == 0) != (period == 0):
                raise ValueError(f'timers: {name}: first and period are set together (both nonzero, or neither)')
            put(words, base, 15, 0, first, f'{name}.first')
            put(words, base + 1, 15, 0, period, f'{name}.period')
            function = spec.get('function', 'hold')
            if function not in functions:
                raise ValueError(f'timers: {name}: unknown function {function}')
            limit, phase = spec.get('resync_limit', 0), spec.get('phase', 0)
            if function != 'resync' and (limit or phase):
                raise ValueError(f'timers: {name}: phase and resync_limit apply to resync only')
            if function == 'resync' and not 1 <= phase <= 0xffff:
                raise ValueError(f'timers: {name}: resync needs a phase of 1-65535 clocks')
            hi, lo = mode_fields['function']
            put(words, base + 2, hi, lo, functions[function], f'{name}.function')
            hi, lo = mode_fields['resync_limit']
            put(words, base + 2, hi, lo, limit, f'{name}.resync_limit')
            put(words, base + 3, 15, 0, phase, f'{name}.phase')
        # Coded shifter registers.
        coder = dict(design.get('coder', {}))
        unknown = set(coder) - {'frame', 'io', 'burst', 'coder', 'coder2'}
        if unknown:
            raise ValueError(f'coder: unknown keys {sorted(unknown)}')
        if 'frame' in coder:
            frame = dict(coder['frame'])
            regs = core['registers']['coder.frame']
            if 'length' in frame:
                length = frame.pop('length')
                if not 1 <= length <= 16:
                    raise ValueError('coder.frame.length is 1-16 bits')
                frame['length_minus_1'] = length - 1
            if 'parity' in frame and isinstance(frame['parity'], str):
                if frame['parity'] not in regs['parity']:
                    raise ValueError(f'coder.frame.parity: one of {sorted(regs["parity"])}')
                frame['parity'] = regs['parity'][frame['parity']]
            self.fields(words, 'coder.frame', frame)
        for key in ('io', 'burst', 'coder', 'coder2'):
            if key in coder:
                self.fields(words, f'coder.{key}', coder[key])
        cf = core['registers']['coder.coder']['fields']
        cw = core['registers']['coder.coder']['word']
        get = lambda name: (words[cw] >> cf[name][1]) & ((1 << (cf[name][0] - cf[name][1] + 1)) - 1)
        if get('stuff_run') == 1 and not get('runs_of_ones'):
            raise ValueError('coder: a stuff run of 1 needs runs_of_ones (an equal-bits stuff bit starts the next run)')
        if get('manchester') and get('nrzi'):
            raise ValueError('coder: Manchester excludes NRZI')
        # CRC registers.
        crc = dict(design.get('crc', {}))
        unknown = set(crc) - {'polynomial', 'residue', 'shape'}
        if unknown:
            raise ValueError(f'crc: unknown keys {sorted(unknown)}')
        for key in ('polynomial', 'residue'):
            if key in crc:
                lo_word, hi_word = core['registers'][f'crc.{key}']['words']
                value = int(crc[key])
                if not 0 <= value < 1 << 32:
                    raise ValueError(f'crc.{key} is 32 bits')
                words[lo_word], words[hi_word] = value & 0xffff, value >> 16
        if 'shape' in crc:
            shape = dict(crc['shape'])
            if 'width' in shape:
                width = shape.pop('width')
                if not 1 <= width <= 32:
                    raise ValueError('crc.shape.width is 1-32 bits')
                shape['width_minus_1'] = width - 1
            if shape.get('link_select'):
                raise ValueError('crc.shape.link_select: a lane core has one coded shifter (link 0)')
            if shape.get('twos_complement') and not shape.get('additive'):
                raise ValueError('crc.shape: twos_complement needs additive')
            # The link carries the bits the coder codes: the sent ones unless
            # the coder receives. A shifter that samples a line with a linked
            # CRC but codes the sent bits sums its idle output, not the frame.
            if shape.get('linked') and 'coder.sample' in connect and not get('receive'):
                raise ValueError('crc.shape.linked on a sampling shifter needs coder.coder.receive '
                                 '(the link carries the coded bits: the sent ones otherwise)')
            self.fields(words, 'crc.shape', shape)
        # Settings: idle actions.
        lane = self.contract['lane']
        idle = design.get('idle_actions', 0)
        hi, lo = lane['idle_actions_bits']
        put(words, lane['idle_actions_word'], hi, lo, idle, 'idle_actions')
        # Every selector names a defined line by construction; reserved bits
        # are never written by `put` through a named field, but check anyway.
        for word, (hi, lo) in core['reserved_bits']:
            if (words[word] >> lo) & ((1 << (hi - lo + 1)) - 1):
                raise ValueError(f'reserved bits {hi}:{lo} of core word {word} set')
        return words


def build(contract, design_path):
    design = json.loads(Path(design_path).read_text())
    if design.get('schema') == 'spatial.lane-design.v1':
        raise ValueError('spatial.lane-design.v1 designs routed the shared grid tiles; lane mode now runs a lane '
                         'core: rewrite the design as spatial.lane-design.v2 (see grid_lane.py)')
    if design.get('schema') != 'spatial.lane-design.v2':
        raise ValueError('expected a spatial.lane-design.v2 design')
    unknown = set(design) - {'schema', 'name', 'description', 'notes', 'connect', 'timers', 'coder', 'crc',
                             'host_words', 'idle_actions', 'program', 'program_base'}
    if unknown:
        raise ValueError(f'unknown design keys {sorted(unknown)}')
    lane = Lane(contract)
    source = (Path(design_path).parent / design['program']).read_text()
    connect = dict(design.get('connect', {}))
    allocated = lane.allocate(source, connect)
    for selector, line in allocated.items():
        if connect.setdefault(selector, line) != line:
            raise ValueError(f'{selector} is connected to {connect[selector]} but the program uses it for {line}')
    words = lane.assemble(source)
    core_words = CoreEncoder(contract).encode(design, connect)
    L = contract['lane']
    core_words[L['program_word']] = lane.program_word(design['program_base'], words)
    # Page 0: the inert grid, lane mode, and the design's host words.
    cfg = contract['configuration']
    image = [0] * (cfg['alternate_context_words'][1] + 1)
    for key, value in L['inert_grid_words'].items():
        image[int(key)] = value
    host = set(L['stream']['words'].values()) | {68}
    for key, value in design.get('host_words', {}).items():
        if int(key) not in host:
            raise ValueError(f'host_words: word {key} is not a host word ({sorted(host)})')
        image[int(key)] = value
    image[L['mode_word']] = 1 << L['mode_bit']
    page = L['core_words_base']
    program = {'schema': 'spatial.lane-program.v2', 'name': design['name'], 'base': design['program_base'],
               'words': words, 'bytes': [b for w in words for b in (w & 255, w >> 8)],
               'core_page': L['core_page'],
               'core_words': {str(page + i): v for i, v in enumerate(core_words)},
               'connect': dict(sorted(connect.items()))}
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
