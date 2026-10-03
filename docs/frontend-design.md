# Frontend contract

The source AST in `include/Frontend/AST.h` describes pin behavior and data
flow without naming a serial protocol or choosing an action lane, SRAM
address, register, or instruction. Protocol libraries can be built for UART, I²C,
SPI, USB line states, JTAG, CAN framing, and Ethernet line coding from the
same operations:

| Need | Frontend nodes |
| --- | --- |
| Pins and buses | `PinDeclaration`, `PinGroupDeclaration`, `PinReadExpr`, `SamplePinsStmt` |
| Drive and release lines | `DriveStmt`, `OutputEnableStmt`, `UpdatePinsStmt` |
| Bit manipulation and framing | `Bits` values, arithmetic, bitwise and comparison expressions, `BitSelectExpr` |
| State and control | `LetStmt`, `AssignStmt`, `IfStmt`, `WhileStmt`, `RepeatStmt`, functions and transactions |
| Time and edges | `DelayForStmt`, `DelayStmt`, `WaitEdgeStmt`, `WaitUntilStmt`, `TimedRegionStmt`, `AtTimeStmt` |
| Data streaming | `ReadQueueStmt`, `WriteQueueStmt` |

For example, UART can drive a start bit, delay for one bit period, then loop
over data bits. I²C and CAN can release an open-drain pin with output enable
and sample it for ACK or arbitration. SPI and JTAG can update clock and data
pins together, then sample on the requested edge. USB differential states can
be represented as paired pin updates plus bit stuffing and line encoding in
ordinary control flow. Ethernet Manchester symbols can be represented by two
timed pin states per bit. These are *representations*, not claims that the
current chip can meet every requested rate or analog electrical requirement.

Physical pin binding and resource limits belong to `TargetProfile`, not the
source AST. The current Tiny Tapeout profile has eight bidirectional user I/O
pins. It also records the present program memory, registers, action lanes,
FIFO depth, and timing clock. A changed chip revision can provide a different
profile while keeping source syntax stable. A future target checker should
report requests that cannot be realized, including a timing offset between
clock ticks, too many simultaneous pin updates, insufficient action slots,
or missing electrical support. The frontend must preserve requested timing
until that check; it must not silently round it.

This repository currently supplies the AST and MLIR dialect scaffolding.
Source parsing, type checking, target validation, scheduling, lowering, and
encoding are separate implementation work. No `.pe` executable is built yet.
