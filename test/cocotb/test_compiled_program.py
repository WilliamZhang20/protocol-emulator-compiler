"""Runs a program encoded by the compiler's assembler on the chip RTL.

Invoked through the chip's test Makefile (see .github/workflows/ci.yml); the
chip's `cocotb_tests` package provides the clock, reset and host helpers.
Program bytes come from the `EmitProgram` binary via $PROGRAM_HEX.
"""

import os

import cocotb
from cocotb.triggers import RisingEdge

from cocotb_tests.common import (
    driven_level,
    host_command,
    read_status,
    reset_top,
    start_clock,
    status_running,
)

PIN = 4


def read_program() -> list[int]:
    with open(os.environ["PROGRAM_HEX"]) as f:
        return [int(line, 16) for line in f if line.strip()]


async def load_program(dut, program: list[int]) -> None:
    for cmd, payload in [(0x1, 0), (0x2, 0), (0x3, 0)]:
        await host_command(dut, cmd, payload)
    for byte in program:
        await host_command(dut, 0x4, byte)
        await host_command(dut, 0x5, byte >> 4)


@cocotb.test()
async def test_compiled_gpio_pulse(dut):
    program = read_program()
    assert program, "assembler emitted no bytes"

    await start_clock(dut)
    await reset_top(dut)
    await load_program(dut, program)
    await host_command(dut, 0x8, 1)  # start engine

    seen = []
    for cycle in range(2000):
        await RisingEdge(dut.clk)
        level = driven_level(dut, PIN)
        if level is not None and (not seen or seen[-1] != level):
            seen.append(level)
        if cycle % 48 == 47 and not status_running(await read_status(dut)):
            break
    else:
        raise AssertionError("engine did not halt")

    # gpio_oe enables the driver while the output latch is still low, then the
    # program writes 1 and 0 again.
    assert seen == [0, 1, 0], f"unexpected pin {PIN} levels: {seen}"
