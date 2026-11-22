#!/usr/bin/env python3
"""Compare direct and ping-pong video feeds on a cycle-shared host link.

This model is intentionally small and explicit. It uses an earliest-deadline
host scheduler, finite TX FIFOs, exact pixel/UART consumption cycles, and a
two-row local store. It excludes controller instructions, GPIO timing beyond
the declared cell cadence, memory read conflicts, and physical timing.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass


@dataclass
class Result:
    mode: str
    host_bytes: int = 0
    channel_switches: int = 0
    video_underflows: int = 0
    uart_underflows: int = 0
    row_deadline_misses: int = 0
    row_writes: int = 0
    video_bytes: int = 0
    uart_bytes: int = 0
    video_fifo_end: int = 0
    uart_fifo_end: int = 0


def simulate(
    *,
    mode: str,
    cells: int,
    visible_lines: int,
    row_repeat: int,
    cell_cycles: int,
    hblank_cycles: int,
    frame_lines: int,
    host_cycles_per_byte: int,
    channel_switch_cycles: int,
    policy: str,
    uart_cycles_per_byte: int,
    fifo_depth: int,
) -> Result:
    if mode not in {"direct", "ping_pong"}:
        raise ValueError("mode must be direct or ping_pong")
    if policy not in {"edf", "sticky"}:
        raise ValueError("policy must be edf or sticky")
    values = (cells, visible_lines, row_repeat, cell_cycles, frame_lines,
              host_cycles_per_byte, uart_cycles_per_byte, fifo_depth)
    if any(value <= 0 for value in values) or hblank_cycles < 0:
        raise ValueError("cycle counts, dimensions, and FIFO depth must be positive")
    if visible_lines % row_repeat or visible_lines > frame_lines:
        raise ValueError("visible lines must divide by row_repeat and fit the frame")

    line_cycles = cells * cell_cycles + hblank_cycles
    frame_cycles = frame_lines * line_cycles
    visible_cycles = visible_lines * line_cycles
    row_period = row_repeat * line_cycles
    rows = visible_lines // row_repeat
    video_outputs = [
        line * line_cycles + cell * cell_cycles
        for line in range(visible_lines)
        for cell in range(cells)
    ]
    uart_outputs = list(range(0, frame_cycles, uart_cycles_per_byte))

    direct = mode == "direct"
    video_fifo = fifo_depth if direct else 0
    uart_fifo = fifo_depth
    video_pops = uart_pops = 0
    video_remaining = max(0, len(video_outputs) - video_fifo)
    uart_remaining = max(0, len(uart_outputs) - uart_fifo)
    result = Result(mode=mode)

    # Preload rows 0 and 1 before the frame. During the frame, refill rows 2
    # through the last row into the inactive bank, then preload rows 0 and 1
    # for the next frame during vertical blanking.
    loaded_rows = {0, 1} if not direct and rows > 1 else ({0} if not direct else set())
    next_row = min(2, rows)
    next_prefetch = 0
    active_row = 0
    row_job: tuple[int | str, int, int] | None = None
    row_job_remaining = 0
    host_end: int | None = None
    host_target: str | None = None
    selected_channel = "video"

    for cycle in range(frame_cycles):
        # Outputs win same-cycle ties with host writes.
        while video_pops < len(video_outputs) and video_outputs[video_pops] == cycle:
            if direct:
                if video_fifo:
                    video_fifo -= 1
                else:
                    result.video_underflows += 1
            else:
                row = video_pops // (cells * row_repeat)
                if row != active_row:
                    loaded_rows.discard(active_row)
                    active_row = row
                    if row not in loaded_rows:
                        result.row_deadline_misses += 1
                # A local store read is assumed to return one byte per cell.
            video_pops += 1

        while uart_pops < len(uart_outputs) and uart_outputs[uart_pops] == cycle:
            if uart_fifo:
                uart_fifo -= 1
            else:
                result.uart_underflows += 1
            uart_pops += 1

        if not direct and cycle == visible_cycles:
            loaded_rows.clear()

        if host_end == cycle:
            if host_target == "video":
                video_fifo += 1
                video_remaining -= 1
                result.video_bytes += 1
            elif host_target == "uart":
                uart_fifo += 1
                uart_remaining -= 1
                result.uart_bytes += 1
            elif host_target == "row":
                assert row_job is not None
                row_job_remaining -= 1
                result.row_writes += 1
                if row_job_remaining == 0:
                    row_id, _, _ = row_job
                    if isinstance(row_id, int):
                        loaded_rows.add(row_id)
                        if row_id == next_row:
                            next_row += 1
                    elif row_id in {"next0", "next1"}:
                        next_prefetch += 1
                    row_job = None
            host_end = None
            host_target = None

        if host_end is not None:
            continue

        candidates: list[tuple[int, int, str]] = []
        if direct and video_remaining and video_fifo < fifo_depth:
            index = video_pops + video_fifo
            if index < len(video_outputs):
                candidates.append((video_outputs[index], 1, "video"))
        if uart_remaining and uart_fifo < fifo_depth:
            index = uart_pops + uart_fifo
            if index < len(uart_outputs):
                candidates.append((uart_outputs[index], 2, "uart"))

        if not direct and row_job is None:
            if next_row < rows:
                release = (next_row - 1) * row_period
                deadline = next_row * row_period
                if release <= cycle < deadline and len(loaded_rows) < 2:
                    row_job = (next_row, release, deadline)
                    row_job_remaining = cells
            elif cycle >= visible_cycles and next_prefetch < 2:
                # These two writes sustain the next frame; their bytes are part
                # of the steady-state frame traffic, even though they are
                # prefetched in this frame's vertical blanking interval.
                row_job = (f"next{next_prefetch}", visible_cycles, frame_cycles)
                row_job_remaining = cells

        if row_job is not None:
            _, _, deadline = row_job
            candidates.append((deadline, 0, "row"))

        if candidates:
            if direct and policy == "sticky":
                same_channel = [
                    candidate for candidate in candidates
                    if ("uart" if candidate[2] == "uart" else "video") == selected_channel
                ]
                host_target = min(same_channel or candidates)[2]
            else:
                host_target = min(candidates)[2]
            channel = "uart" if host_target == "uart" else "video"
            switch = channel_switch_cycles if channel != selected_channel else 0
            result.channel_switches += int(switch > 0)
            selected_channel = channel
            host_end = cycle + switch + host_cycles_per_byte

    result.host_bytes = result.video_bytes + result.uart_bytes + result.row_writes
    result.video_fifo_end = video_fifo
    result.uart_fifo_end = uart_fifo
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cells", type=int, default=80)
    parser.add_argument("--visible-lines", type=int, default=480)
    parser.add_argument("--row-repeat", type=int, default=8)
    parser.add_argument("--cell-cycles", type=int, default=16)
    parser.add_argument("--hblank-cycles", type=int, default=320)
    parser.add_argument("--frame-lines", type=int, default=525)
    parser.add_argument("--host-cycles-per-byte", type=int, default=15)
    parser.add_argument("--channel-switch-cycles", type=int, default=0)
    parser.add_argument("--policy", choices=("edf", "sticky"), default="edf")
    parser.add_argument("--uart-cycles-per-byte", type=int, default=20)
    parser.add_argument("--fifo-depth", type=int, default=8)
    args = parser.parse_args()

    frame_cycles = args.frame_lines * (args.cells * args.cell_cycles + args.hblank_cycles)
    capacity = frame_cycles // args.host_cycles_per_byte
    uart_bytes = frame_cycles // args.uart_cycles_per_byte
    video_bytes = args.cells * args.visible_lines
    row_bytes = video_bytes // args.row_repeat
    print(f"frame={frame_cycles} cycles; shared host capacity={capacity} bytes/frame")
    print(f"steady demand: direct={uart_bytes + video_bytes}; ping_pong={uart_bytes + row_bytes} bytes/frame")
    for mode in ("direct", "ping_pong"):
        result = simulate(
            mode=mode,
            cells=args.cells,
            visible_lines=args.visible_lines,
            row_repeat=args.row_repeat,
            cell_cycles=args.cell_cycles,
            hblank_cycles=args.hblank_cycles,
            frame_lines=args.frame_lines,
            host_cycles_per_byte=args.host_cycles_per_byte,
            channel_switch_cycles=args.channel_switch_cycles,
            policy=args.policy,
            uart_cycles_per_byte=args.uart_cycles_per_byte,
            fifo_depth=args.fifo_depth,
        )
        print(
            f"{mode}: host writes={result.host_bytes}, channel switches={result.channel_switches}, "
            f"video bytes={result.video_bytes}, "
            f"row writes={result.row_writes}, UART bytes={result.uart_bytes}; "
            f"underflows video={result.video_underflows}, UART={result.uart_underflows}, "
            f"row deadline misses={result.row_deadline_misses}; "
            f"final FIFOs video={result.video_fifo_end}, UART={result.uart_fifo_end}"
        )
    print(
        f"Assumptions: prefilled FIFOs/rows; policy={args.policy}; "
        f"channel switch cost={args.channel_switch_cycles}; no instruction or physical memory delays."
    )


if __name__ == "__main__":
    main()
