#!/usr/bin/env python3
"""Split a sequencer program into 16-row pages held in SRAM.

The hardware (protocol-emulator `grid_sequencer`) executes one 16-row page
from registers. With paging enabled (word 94) the pages live in the host
SRAM bank; a page row `{1,1,00,page,entry,action}` holds `action` while the
named page loads, then continues at `entry`. A page load takes about 45
clocks, so a page boundary may only be placed in front of a row whose entry
may be delayed while its own action is held: the lowering lists those rows
as `stretchable_rows`.

Pages are contiguous ranges of the flat program. An edge that leaves its
page must target a stretchable row (through a local page row) or a halt row
(copied into the page). An edge entering a page from elsewhere must likewise
target a stretchable or halt row. The fewest pages are found by dynamic
programming over range ends.
"""

PAGE_ROWS = 16
MAX_PAGES = 16
PAGE_STRIDE = 64
HOST_BANK_BYTES = 1024
FAIL_ROW = 0x8004  # halt with the error flag: unreachable padding fails closed


def pack(row):
    halt, no, yes, event, action = row
    if not (0 <= no < 16 and 0 <= yes < 16 and 0 <= event < 8 and 0 <= action < 16 and halt in (0, 1)):
        raise ValueError(f'row does not fit the hardware encoding: {row}')
    return (halt << 15) | (no << 11) | (yes << 7) | (event << 4) | action


def successors(row):
    halt, no, yes, _, _ = row
    return set() if halt else {no, yes}


def _plan_page(program, start, end, stretchable):
    """Return (local rows needed, halt copies, trampolines) or None."""
    halts, jumps = [], []
    inside = range(start, end)
    for r in inside:
        for t in sorted(successors(program[r])):
            if not 0 <= t < len(program):
                raise ValueError(f'row {r} jumps outside the program')
            if start <= t < end:
                continue
            if program[t][0]:
                if t not in halts:
                    halts.append(t)
            elif t in stretchable:
                if t not in jumps:
                    jumps.append(t)
            else:
                return None
    for r in range(len(program)):
        if start <= r < end:
            continue
        for t in successors(program[r]):
            if start <= t < end and not program[t][0] and t not in stretchable:
                return None
    return (end - start) + len(halts) + len(jumps), halts, jumps


def paginate(program, entry, stretchable, page_rows=PAGE_ROWS):
    """Return None if one page suffices, else a list of 16-row packed pages."""
    stretchable = set(stretchable)
    n = len(program)
    if n <= page_rows:
        return None
    if entry != 0:
        raise ValueError('a paged program must enter at row 0 (page 0 loads at start)')
    if not 1 <= page_rows <= PAGE_ROWS:
        raise ValueError('page_rows must be 1..16')
    best = [None] * (n + 1)  # best[i]: (pages, previous end) covering rows [0, i)
    best[0] = (0, None)
    for end in range(1, n + 1):
        for start in range(end):
            if best[start] is None:
                continue
            plan = _plan_page(program, start, end, stretchable)
            if plan is None or plan[0] > page_rows:
                continue
            cost = best[start][0] + 1
            if best[end] is None or cost < best[end][0]:
                best[end] = (cost, start)
    if best[n] is None:
        raise ValueError(f'{n}-row program has no legal page split: page boundaries need stretchable rows')
    bounds, end = [], n
    while end:
        start = best[end][1]
        bounds.append((start, end))
        end = start
    bounds.reverse()
    if len(bounds) > MAX_PAGES:
        raise ValueError(f'program needs {len(bounds)} pages; the hardware holds {MAX_PAGES}')
    home = {r: (p, r - s) for p, (s, e) in enumerate(bounds) for r in range(s, e)}
    pages = []
    for p, (start, end) in enumerate(bounds):
        _, halts, jumps = _plan_page(program, start, end, stretchable)
        local = {r: r - start for r in range(start, end)}
        for k, t in enumerate(halts):
            local[t] = end - start + k
        for k, t in enumerate(jumps):
            local[t] = end - start + len(halts) + k
        rows = []
        for r in range(start, end):
            halt, no, yes, event, action = program[r]
            rows.append(pack(program[r]) if halt else pack((0, local[no], local[yes], event, action)))
        rows += [pack(program[t]) for t in halts]
        for t in jumps:
            page, index = home[t]
            rows.append(0xC000 | page << 8 | index << 4 | program[t][4])
        rows += [FAIL_ROW] * (PAGE_ROWS - len(rows))
        pages.append(rows)
    return pages


def page_bytes(pages, contexts):
    """SRAM image: per page, 16 rows then 5 context words, little-endian."""
    if len(contexts) != 5:
        raise ValueError('five alternate-context words per page')
    out = []
    for p, rows in enumerate(pages):
        words = list(rows) + list(contexts)
        data = [b for w in words for b in (w & 255, w >> 8)]
        out.append((PAGE_STRIDE * p, data))
    return out


def page_base(page_count):
    """Pages occupy the top of the host bank."""
    return HOST_BANK_BYTES - PAGE_STRIDE * page_count
