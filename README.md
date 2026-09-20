# protocol-emulator-compiler

Assembler and compiler frontend for
[protocol-emulator](https://github.com/WilliamZhang20/protocol-emulator),
an SRAM-programmed protocol engine (Tiny Tapeout). Protocol behavior is
bytecode in foundry SRAM — UART, SPI, and I²C are programs, not fixed
peripherals.

This repository is the software side of that ISA. It is **not** a copy of
the RTL tree and does **not** include a partitioner, scheduler, codegen
backend, FPGA loader, or host/debug tooling.

## Reference

ISA, host nibble commands, fetch timing (`WAIT16 + 11`), the 8×16 RF + ALU,
event bits, and the Phase C/D action engine are defined in protocol-emulator:

- [docs/architecture.md](https://github.com/WilliamZhang20/protocol-emulator/blob/main/docs/architecture.md)
  — architecture, bytecode encodings, action words `{op[3:0], args[11:0]}`
- [test/cocotb_tests/reference/programs.py](https://github.com/WilliamZhang20/protocol-emulator/blob/main/test/cocotb_tests/reference/programs.py)
  — current Python assembler helpers (`gpio_write`, `wait`, `uart_tx_program`, …)

Module names and encodings here follow those sources. Do not add
protocol-shaped opcodes; new ISA work belongs on the action-engine path.

Pipeline:

```text
.pe source  ── pec ──► AST ──► frontend IR
                              │
                              │  TODO: typecheck, DJNZ loops, action packing
                              ▼
                         (codegen backend — not here)
                              │
.peas asm   ── pec-asm ───────► SRAM bytes ──► host load (protocol-emulator)
```

## Build

Will use MLIR in C++ for everything. OCaml will be deleted.

Why you may ask? Because MLIR is better for heterogenous sytems. It is also more established and has a better API.

Mnemonics match `programs.py` / architecture.md:

| Mnemonic | Encoding |
| --- | --- |
| `nop` / `halt` | `00` / `01` |
| `wait16 N` | `10 ll hh` |
| `gpio_write PIN, VAL` / `gpio_oe PIN, EN` | `2vppp` / `3vppp` |
| `tx_load` / `rx_push` | `40` / `70` |
| `shift_out PIN` / `shift_in PIN` | `5p` / `6p` |
| `jmp` / `jz` / `jnz LAB` | `80`–`82` + 10-bit address |
| `djnz Rn, LAB` | `88+n ll hh` |
| `set Rn, IMM8` / `mov Rd, Rs` | `AA` / `AB` |
| `add`/`sub`/`and`/`or`/`xor`/`shl`/`shr Rd, Rs` | `AC op rsrd` |
| `wait_event MASK` | `D0 mask` (`EV_XFER_DONE`, `EV_TIMER_DONE`, `EV_REGION_DONE`, …) |
| `start_xfer clk=…, tx=…, rx=…, bits=…, half=…` | `Cppp cfg pins half` (legacy Phase B) |
| `run_region` / `wait_region` / `prog_action` | Phase D CPU ↔ action interface |

Registers are `r0`–`r7`. Jumps take labels. `#` is not a comment in
assembly; use `;`.

## Frontend (`.pe`)

A small engine language over the same resources: named pins, `loop` /
`repeat`, FIFO/GPIO/shift/wait, and `region { action … }` stubs.

```pe
engine uart_tx {
  pin tx = 0;
  imm bit_wait = 423;
  gpio_oe(tx, 1);
  loop {
    tx_load;
    gpio_write(tx, 0);
    wait16(bit_wait);
  }
}
```

`pec` only parses and lowers to IR. It does **not** produce SRAM bytes.

## Out of scope

Intentionally omitted (grow later, do not land as empty stubs):

- Codegen backend (IR → `.peas` / bytes)
- Partitioner and scheduler
- FPGA / host loader, serial debug, waveform tools
- Test-harness ports of the cocotb suite

## TODO

- Typecheck pin/register/event names in the frontend
- Lower `repeat` to `DJNZ` instead of unrolling
- Pack Phase C action words (`ACT_GPIO` / `ACT_DELAY` / `ACT_DONE` / …)
- Optional listing of per-opcode cycle cost (`3 + 2×operands`, plus stalls)
