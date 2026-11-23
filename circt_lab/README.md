# CIRCT architecture and frontend experiment

This is an experimental compiler path, not a replacement for the existing Rust
compiler. It connects the existing frontend to registered MLIR operations and
uses a CIRCT FSM in a narrow, working RTL-image path. It also analyzes a family
of hypothetical selector topologies. Frontend-derived Boolean dependencies can
be analyzed against a hypothetical graph. A restricted control-only `.spt`
program now produces an image for a separate control-fabric RTL prototype.
An SPI timer/shifter component can also be derived from a real source program;
its transaction controller and payload remain supplied by the experiment.
Do not treat frontend export
or topology analysis as compilation/fit.

The compiler's legacy example CI runs against the pinned, ABI-2 actor RTL
experiment (`c053034`), because the older default chip revision lacks that
actor ABI. That legacy regression is separate from this CIRCT fabric path and
does not validate the new two-SRAM placement top.

## Existing frontend and registered dialect

`spatial frontend-mlir FILE.spt` invokes the existing Rust parser and logical
program extraction, without allocating the old hardware. It produces a
`spatial.frontend_program` containing ordered `spatial.frontend_item` operations
for the original logical declarations and `spatial.frontend_input` and
`spatial.frontend_stream` operations for the frontend's connection graph. Each retains the
frontend's JSON data, including available source spans, expressions, checks and
context information. The enclosing operation preserves clock, protocol machines,
the frontend's stated limitations and connection-graph limitations. A test
round-trips the UART program and connection graph through MLIR and checks
equality. This reuses the frontend; it does not
invent a second `.spt` parser.

For each explicit source protocol state machine the exporter additionally emits
a typed, registered `spatial.protocol` operation. Its verifier checks inputs,
state and transition bounds, duplicate rows, actions, and consistency with the
original frontend protocol. `--control-rows` lowers one selected protocol into
the prototype's 16-row, four-state/two-input control table. It removes
unreachable states and merges equivalent synchronous Mealy states, preserving
the source's implicit hold/no-action rows. The report includes the state map
and explicitly marks input binding and full-program compilation as unverified.
An actual `.spt` example, `examples/control.spt`, is lowered this way and its
generated table is exercised on the RTL control tile. This is a component
lowering, not routing the source program or compiling a complete protocol.

`--compile-control` goes further for a narrow complete program: one clock,
one explicit protocol, one or two input pins, and output pins driven by
registers capturing distinct protocol actions. It validates every declaration
and connection before emitting the control table and pad-enable mask. Pin
bindings are reported in JSON. The separate `control_fabric` RTL implements
this exact target, with synchronized inputs and registered outputs; it is not
the existing chip or a general heterogeneous fabric. Other source constructs
are rejected. `examples/control_full.spt` is an end-to-end example.

`--derive-spi-component FRONTEND TIMER SHIFTER PAYLOAD` extracts one specific
source pattern: an idle-low timer driven by shifter busy until done, an
MSB-first data-only frame, falling-edge shifts and rising-edge pin sampling.
It resolves a literal cycle period or one named cycle constant, and emits a
`spatial.spi` workload for the existing SPI prototype mapper. The source's
transaction functions, start expression, output pin binding, other resources,
streams, and tests are not lowered. The supplied payload and hand-written
control FSM are experiment inputs. This is a component experiment, not a
complete compilation of `examples/spi.spt`. Unsupported widths, periods,
frame formats, and clocking relationships are rejected.

`--compile-memory-spi FRONTEND PAYLOAD OUTPUT` targets the integrated
`memory_spi_fabric` experiment. It accepts the strict, explicitly declared
control/datapath pattern in `examples/memory_spi.spt`: its control table comes
from the source `spatial.protocol`, and the timer, shifter, CS, and signal
routes are checked against the implemented ABI. The first 18 words configure
the fabric; the nineteenth is a left-aligned example payload. The test places
that payload in SRAM through the host port. Changing the source protocol rows
changes the control configuration. The compiler rejects unsupported source
items and mismatched pin or control bindings before creating an image.

This is a source-derived control/datapath component, **not a complete `.spt`
transaction compiler**: SRAM preload, bank/address allocation, trigger staging,
and byte transfers still use a hand-written hardware adapter and test driver.
Functions, general streams, arbitrary topology, and cycle-accurate source
timing are not lowered. The source `trigger` is staged until the adapter has
prefetched the word. The result reports `full_program_compiled: false`.
In particular, the legacy backend rejects host-exchanged words wider than
eight bits; its `bytes via bits` calling convention is not evidence for how
12-bit words should be packed. The next compiler step is an explicit typed
word/stream representation and a specified host-to-SRAM packing contract,
before claiming a general source transaction function has been compiled.

`dialect.cpp` registers these operations, `spatial.fabric`, `spatial.spi`,
`spatial.config_write`, and `spatial.payload` as a real loadable MLIR dialect.
Their verifiers check structural invariants such as operation shape, integer
and result types, duplicate selectors, malformed JSON, and inconsistent
frontend item/connection envelopes. In particular the frontend verifier
preserves `valid`, `invalid`, and `unknown` input-check statuses; it **does not**
turn unresolved/invalid input into a semantically valid program. Many frontend
fields are still encoded in JSON attributes, not typed operations. Full semantic
checking and source-to-protocol lowering remain to be implemented.

`circt-opt --load-dialect-plugin=...` verifies these operations without
`--allow-unregistered-dialect`. The C++ mapper registers the same plugin. The
registered CIRCT `fsm.machine` in `control_fsm.mlir` is parsed and verified by
CIRCT and consumed by the current narrow SPI mapping. The FSM represents state
transitions; the mapper still supplies SPI-specific registered actions, so
this is not a general Mealy-machine lowering.

## Flexible candidate analysis and exact RTL emission

### Typed program and resource graphs

`--memory-spi-graph FRONTEND` first validates the same restricted source used
by `--compile-memory-spi`, then emits a registered `spatial.workload_graph`.
Its ports have explicit MLIR integer types. The 12-bit example includes the
source-derived control table and timer period, two i8 reads, explicit
big-endian low-bit packing to i12, control and timer events, separate rise and
fall signals, a 12-bit shift, unpacking, two i8 writes, feedback, and I/O.
The host's source/destination addresses (100/101 and 200/201) and the memory
adapter milestones come from the RTL test, not from `.spt`; the graph records
that provenance. This is not frontend lowering for general transactions.

```text
trigger ─┬─> read[100] ─order─> read[101] ─┐
         │        └──────── i8 ─────────────┼─> pack i12 ─┬─> shift i12 ─> unpack ─> write[200] ─order─> write[201]
         └──────────────────────────────────┴─> control ──┤      │
                                             timer ◄──────┴──────┤ busy
                                               └──── rise/fall ──┘
```

The graph also contains MISO, MOSI, SCK and the inverted-busy CS path. Its
feedback edges describe connections; they are not one-shot dependencies.

`spatial.target_graph` separately describes tiles, supported operation names,
widths, optional memory depths, single-port occupancy/latency, and directed
links. `--map-graph TARGET WORKLOAD` performs bounded first-feasible placement,
checks typed edge widths, tile capabilities, memory addresses and groups,
routes each dependency through explicit directed links and route tiles, and
builds a simple dependency/resource schedule. A provisional nominal pad
frequency check is performed only when both graphs supply the relevant values.
The graph operations have registered MLIR verifiers and work with `circt-opt`.

The result is **model-only**: it does not emit configuration and does not
prove the topology matches RTL. The test target is an abstraction of the
current slice, not a fully verified chip description. Each operation in the
source example uses a one-cycle abstract duration, including the multi-cycle
shifter, so its reported makespan is *not* transaction latency. Feedback is
routed but not scheduled; selector conflicts, concurrent link bandwidth,
value lifetime/storage allocation, memory arbitration against host traffic,
physical timing, and deadlines are
not yet modeled. The scheduler checks port conflicts for requests actually
present in the workload; an independent two-request fixture tests same-bank
serialization versus two-bank concurrency. It is a first-feasible mapper,
not a hill-climbing optimizer or evidence of full program fit.

For example:

```sh
./target/debug/spatial frontend-mlir circt_lab/examples/memory_spi.spt > /tmp/source.mlir
/tmp/spatial-circt-map --memory-spi-graph /tmp/source.mlir > /tmp/workload.mlir
/tmp/spatial-circt-map --map-graph circt_lab/candidates/memory-spi-graph.mlir /tmp/workload.mlir
```

The existing Rust `spatial check` / `spatial build` hardware map is retained
for the older target. For this graph mapper, save its JSON output and run
`python3 circt_lab/show_graph.py report.json` (or pipe JSON to the script).
It shows every modeled tile, including unused tiles, assigned operations, and
the chosen routes. These are model assignments, not measured utilization;
multiple operations on a tile need not be active concurrently.

The graph tests compare the source-derived control rows and packing with the
RTL-tested image; the standalone RTL-only test also checks the adapter's
read-100, read-101, write-200, write-201 request sequence. They check alternative bank placement and multihop routes,
exercise resource latency versus initiation interval, and reject unsupported
widths, operations, addresses, links, pad estimates and mismatched port types.
Sixty seeded small placement cases are checked against an independent
exhaustive feasibility oracle. This cross-checks the graph's inputs against the working experiment; it is not
a cycle-by-cycle proof of the abstract schedule against RTL.

`spatial-circt-map --analyze ARCH.mlir PROGRAM.mlir` considers the supplied
selector topology, capacities and workload connections without assuming the
prototype's selector order. A selector may take a source or an explicit
`route.<sink>` relay. A bounded backtracking search assigns selectors across all
requested connections and reports the chosen routes and selector-hop cycles.
It can explore changed source ordering, extra relay junctions and changed width
or storage capacities. The `candidates/backtrack*` fixture requires rerouting an
earlier demand to let another use a shared selector.

The same analysis accepts a `spatial.frontend_program` exported by the Rust
frontend. It currently handles only base-context Boolean inputs that are a
simple name or field with one resolved reference. It rejects compound logic,
constants, contexts and unresolved references rather than silently discarding
their semantics. Its candidate route names refer to **logical names**, not
placed physical tile ports. `candidates/loopback-boolean.mlir` routes ten
Boolean dependencies extracted from `examples/loopback.spt`, including one
two-hop route. The report explicitly counts two unmodeled word streams; it is
not a statement that the full program fits.

This model is intentionally limited: the hand-authored SPI mode assumes one
shifter and one store; frontend mode does not check resource fit. Both use
Boolean selector routes with a uniform per-hop cycle cost and assume unlimited
fanout. Neither places tiles or models shared-link traffic capacity, word-wide
links, memory-port arbitration, concurrency, protocol correctness, physical
wire delay, or input-to-output deadlines. It explores at most 64 routes and
100,000 search steps; reaching the limit reports unknown feasibility. Reports
explicitly state that RTL fidelity is unverified and no image was emitted.

The separate image-emission mode targets exactly the **implemented prototype
ABI**. It requires the actual RTL's ordered selector wiring, 16-bit shift
width, four-word register file, and four-state/two-input control capacity. It
rejects incompatible hypothetical changes instead of emitting a misleading
bitstream. `prototype.mlir`, `spi.mlir`, and `control_fsm.mlir` describe the
initial narrow SPI case. The mapper emits 18 configuration words and a TX
payload, plus inspectable registered MLIR configuration operations. The
hardware repository's `circt_image_tb.v` loads those words at the configuration
and payload ports and checks pin behavior. The mapper does not generate RTL.

The user-provided pad assumptions are provisional estimates: approximately
66 MHz maximum output frequency and 10 ns round trip through pads and mux,
not measured CMOS5L silicon results. The mapper checks a nominal output
frequency against the estimate; it does not use 10 ns as proof of any
input-to-output timing guarantee.

## Build and run

Install a compatible CIRCT distribution with C++ headers and shared libraries.
The local experiment used official `firtool-1.164.0`; the downloaded archive's
checksum matched the release checksum. This machine needed a newer glibc loader
and a Z3 compatibility library path, which are system-specific.

```sh
cargo build --locked --bin spatial
CIRCT_PREFIX=/path/to/circt OUT=/tmp/spatial-circt-map circt_lab/build.sh
SPATIAL_DIALECT=/tmp/libSpatialDialect.so
./target/debug/spatial frontend-mlir examples/spi.spt > /tmp/frontend.mlir
/path/to/circt-opt --load-dialect-plugin="$SPATIAL_DIALECT" /tmp/frontend.mlir -o /dev/null
/tmp/spatial-circt-map --analyze circt_lab/prototype.mlir circt_lab/spi.mlir
./target/debug/spatial frontend-mlir examples/loopback.spt > /tmp/loopback.mlir
/tmp/spatial-circt-map --analyze circt_lab/candidates/loopback-boolean.mlir /tmp/loopback.mlir
./target/debug/spatial frontend-mlir circt_lab/examples/control.spt > /tmp/control.mlir
/tmp/spatial-circt-map --control-rows /tmp/control.mlir handshake /tmp/control.mem
make -C ../protocol-emulator tile-control-image-test IMAGE=/tmp/control.mem
./target/debug/spatial frontend-mlir examples/spi.spt > /tmp/source-spi.mlir
/tmp/spatial-circt-map --derive-spi-component /tmp/source-spi.mlir sck_clock bits 165 > /tmp/source-component.mlir
/tmp/spatial-circt-map circt_lab/prototype.mlir /tmp/source-component.mlir \
  circt_lab/control_fsm.mlir /tmp/source-component.mem > /tmp/source-mapped.mlir
make -C ../protocol-emulator tile-circt-image-test IMAGE=/tmp/source-component.mem
./target/debug/spatial frontend-mlir circt_lab/examples/memory_spi.spt > /tmp/memory-spi.mlir
/tmp/spatial-circt-map --compile-memory-spi /tmp/memory-spi.mlir 2652 /tmp/memory-spi.mem
make -C ../protocol-emulator tile-memory-spi-image-test IMAGE=/tmp/memory-spi.mem
./target/debug/spatial frontend-mlir circt_lab/examples/control_full.spt > /tmp/control-full.mlir
/tmp/spatial-circt-map --compile-control /tmp/control-full.mlir /tmp/control-full.mem
make -C ../protocol-emulator control-fabric-image-test IMAGE=/tmp/control-full.mem
/tmp/spatial-circt-map circt_lab/prototype.mlir circt_lab/spi.mlir \
  circt_lab/control_fsm.mlir /tmp/spi.mem > /tmp/mapped.mlir
make -C ../protocol-emulator tile-circt-image-test IMAGE=/tmp/spi.mem
```

Run the regression with executable paths for the mapper, CIRCT tool and plugin:

```sh
CIRCT_MAP=/tmp/spatial-circt-map CIRCT_OPT=/path/to/circt-opt \
SPATIAL_DIALECT=/tmp/libSpatialDialect.so circt_lab/test.sh
```

Set `SPI_IMAGE_TB` to the absolute path of
`../protocol-emulator/build/verilator/tile_circt_image/Vcirct_image_tb` to run
all generated 1-, 5-, 8-, 12- and 16-bit images through the RTL pin peer. If it
is absent, the script reports RTL as skipped. Likewise `CONTROL_IMAGE_TB`
selects the executable built by `tile-control-image-test`; without it the
source-to-control RTL case is reported as skipped. The tests also verify real Rust
frontend exports, malformed registered operations, alternate topologies,
selector conflicts, unsupported image targets, and the registered CIRCT FSM.
They compare 30 seeded small selector graphs against exhaustive search,
including satisfiable and conflicting cases. This validates the solver on
those graphs, not the physical fidelity of the model. Forty seeded source
control graphs are also compared against the existing Rust logical-state
reference, including every reachable table row and equivalence class.
This is not a regression of the full existing chip.
Set `FULL_CONTROL_IMAGE_TB` to the executable built by
`control-fabric-image-test` to exercise the complete restricted program.
For the memory-backed source component, set `MEMORY_SPI_IMAGE_TB` to the
executable built by `tile-memory-spi-image-test` and `MEMORY_SPI_FIXTURE` to
the hardware repository's `test/data/spi12.mem`. The regression checks that
the freshly compiled image matches the fixture, runs it through the SRAM-backed
fabric and signal routes, and verifies a source-control mutation prevents the
reference transfer.

## Corpus coverage

Run `python3 circt_lab/corpus.py --help` for the required tool paths and JSON
output options. It exports and verifies every `.spt` in the selected directory,
then tries the restricted typed-workload lowering and graph mapping, each explicit protocol graph on the control tile and independently
checks emitted tables against source semantics. It probes each declared
timer/shifter pair against the restricted SPI component pattern. It also attempts complete
mapping to the restricted control fabric, recording per-program rejection reasons.
`--require-full` exits nonzero until every full program has a real mapping.

The current 35-example WIP corpus yielded 35 frontend exports and dialect
verifications. Five programs declare explicit protocol graphs: two component
tables were emitted and checked, and three minimized graphs exceeded four
states. **Zero complete programs were mapped.** Most examples describe their
protocol through timers, shifters, functions and streams rather than explicit
protocol graphs; they need semantic lowering before the new architecture's
coverage or topology can be evaluated. The typed-graph probe reports lowering
unsupported separately from mapping failure and abstract placement; none means
whole-program fit or hardware equivalence. These numbers are a pipeline coverage
snapshot, not a finding that the candidate fabric fits or fails the protocols.
The corpus now also probes an experimental UART 8N1 structural lowering.
It builds a typed graph with separate TX and RX, frame operations, the RX
falling-edge detector, host streams and source-derived first-tick timing.
This narrow prototype currently runs in `lower_uart.py` over the verified
frontend JSON and emits registered MLIR; it is not yet the intended CIRCT
lowering pass. It marks the graph `analysis_only`, and the mapper refuses it:
operation behavior, timing, host transport, selectors and RTL implementation
have not been established. For example:

```sh
target/debug/spatial frontend-mlir examples/uart.spt > /tmp/uart.mlir
python3 circt_lab/lower_uart.py --mapper /tmp/spatial-circt-map /tmp/uart.mlir > /tmp/uart-graph.mlir
/tmp/spatial-circt-map --verify /tmp/uart-graph.mlir
```

The current baseline corpus yields one `structural_graph_only` (UART) and 34
`lowering_unsupported` results. In `circt_lab/examples`, the restricted
`memory_spi.spt` graph receives an abstract placement, while two control
examples cannot yet use that lowering. This is a compiler coverage result,
not a protocol-support verdict. The baseline USB example is a low-speed
packet transmitter and the Ethernet examples are 10BASE-T transmitters;
their presence is not evidence of full USB or Ethernet support.
The restricted timer/shifter pattern matched one of 80 pairs probed across the
corpus: the timer and shifter in `spi.spt`. That count measures this extractor's
coverage, not how many protocols the hardware could support.

The next compiler milestone is to lower more checked frontend operations into
typed operations, then place and route those operations onto
explicit heterogeneous tiles. Keep exact-target image emission separate from
hypothetical candidate analysis until each candidate has corresponding RTL and
calibration. The upstream [CIRCT FSM dialect](https://circt.llvm.org/docs/Dialects/FSM/)
and [MLIR dialect conversion framework](https://mlir.llvm.org/docs/DialectConversion/)
provide relevant infrastructure; they do not by themselves prove a protocol
mapping or replace the target-specific verifier.
