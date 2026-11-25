#!/usr/bin/env python3
"""Paged sequencer programs behave like the flat program they came from.

A page row only adds delay, so with page rows treated as transparent the
paged program must make the same row-by-row actions, halts and errors as
the flat program for any event sequence.
"""
import random
import grid_pager as gp

# The compiled I2C write_read program (temporal_functions.inc), flat form.
I2C = [[0,1,1,0,7],[0,1,2,1,2],[0,2,3,1,0],[0,4,4,0,4],[0,5,5,0,12],[0,5,6,6,8],
       [0,7,9,7,8],[0,8,9,5,8],[0,4,14,3,8],[0,9,10,1,0],[0,10,11,1,2],[0,13,12,7,3],
       [1,0,0,0,3],[1,0,0,0,7],[0,14,15,1,1],[0,15,1,1,3]]
I2C_STRETCH = [1, 3, 9, 14]


def run_flat(program, events, steps):
    trace, r = [], 0
    for k in range(steps):
        halt, no, yes, event, action = program[r]
        trace.append((action, halt))
        if halt:
            break
        r = yes if events[k][event] else no
    return trace


def run_paged(pages, events, steps):
    trace, page, r, k, loads = [], 0, 0, 0, 0
    while k < steps:
        row = pages[page][r]
        if row >> 14 == 3:
            assert row >> 12 & 3 == 0
            page, r = row >> 8 & 15, row >> 4 & 15
            loads += 1
            assert loads < 10 * steps, 'page rows loop without executing a row'
            continue
        halt, no, yes, event, action = row >> 15, row >> 11 & 15, row >> 7 & 15, row >> 4 & 7, row & 15
        trace.append((action, halt))
        if halt:
            break
        r = yes if events[k][event] else no
        k += 1
    return trace


def equivalent(program, stretchable, page_rows, trials=400, steps=200):
    pages = gp.paginate(program, 0, stretchable, page_rows)
    assert pages is not None
    assert all(len(p) == 16 for p in pages)
    rng = random.Random(page_rows)
    for _ in range(trials):
        events = [[1] + [rng.random() < 0.3 for _ in range(7)] for _ in range(steps)]
        assert run_paged(pages, events, steps) == run_flat(program, events, steps)
    return pages


def long_program(n, stretch_every):
    """A chain of waits with a loop back to row 1, ending in success/failure halts."""
    rows, stretch = [], []
    for i in range(n):
        if i == n - 2:
            rows.append([1, 0, 0, 0, 3]); continue
        if i == n - 1:
            rows.append([1, 0, 0, 0, 7]); continue
        nxt = i + 1 if i + 1 < n - 2 else n - 2
        fail = n - 1 if i % 5 == 4 else i          # some rows fail, others wait
        rows.append([0, fail, nxt, 1 + i % 7, i % 16])
        if i % stretch_every == 1:
            stretch.append(i)
    rows[n - 3][1] = 1                              # a back edge to row 1
    return rows, stretch


def main():
    assert gp.paginate(I2C, 0, I2C_STRETCH) is None, 'a 16-row program stays unpaged'
    pages = equivalent(I2C, I2C_STRETCH, 12)
    assert len(pages) == 2
    for size in (8, 10, 14):
        equivalent(I2C, I2C_STRETCH, size)
    for n in (20, 40, 100, 150):
        rows, stretch = long_program(n, 3)
        pages = equivalent(rows, stretch, 16)
        assert len(pages) <= gp.MAX_PAGES
    # Without stretchable rows there is no legal split.
    try:
        gp.paginate(I2C, 0, [], 12)
    except ValueError as error:
        assert 'stretchable' in str(error)
    else:
        raise AssertionError('split without stretchable rows')
    # A paged program enters at row 0.
    try:
        gp.paginate(long_program(30, 3)[0], 1, long_program(30, 3)[1])
    except ValueError:
        pass
    else:
        raise AssertionError('nonzero entry accepted')
    # Too many pages for the hardware.
    try:
        rows, stretch = long_program(400, 2)
        gp.paginate(rows, 0, stretch)
    except ValueError as error:
        assert 'pages' in str(error)
    else:
        raise AssertionError('more than 16 pages accepted')
    data = gp.page_bytes([[0x1234] * 16, [0x8004] * 16], [1, 2, 3, 4, 5])
    assert data[0] == (0, [0x34, 0x12] * 16 + [1, 0, 2, 0, 3, 0, 4, 0, 5, 0]) and data[1][0] == 64
    assert gp.page_base(2) == 896
    print('Sequencer pager: equivalence on I2C (8-14 row pages) and 20-150 row programs, rejections passed')


if __name__ == '__main__':
    main()
