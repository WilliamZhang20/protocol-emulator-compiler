# CIRCT architecture and frontend experiment

This is an experimental compiler path, not a replacement for the existing Rust
compiler. It connects the existing frontend to registered MLIR operations and
uses a CIRCT FSM in a narrow, working RTL-image path. It also analyzes a family
of hypothetical selector topologies. Frontend-derived Boolean dependencies can
be analyzed against a hypothetical graph. A restricted control-only `.spt`
program now produces an image for a separate control-fabric RTL prototype; the
SPI image still uses a hand-authored workload. Do not treat frontend export
or topology analysis as compilation/fit.

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

## Corpus coverage

Run `python3 circt_lab/corpus.py --help` for the required tool paths and JSON
output options. It exports and verifies every `.spt` in the selected directory,
then tries each explicit protocol graph on the control tile and independently
checks emitted tables against source semantics. It also attempts complete
mapping to the restricted control fabric, recording per-program rejection reasons.
`--require-full` exits nonzero until every full program has a real mapping.

The current 35-example WIP corpus yielded 35 frontend exports and dialect
verifications. Five programs declare explicit protocol graphs: two component
tables were emitted and checked, and three minimized graphs exceeded four
states. **Zero complete programs were mapped.** Most examples describe their
protocol through timers, shifters, functions and streams rather than explicit
protocol graphs; they need semantic lowering before the new architecture's
coverage or topology can be evaluated. These numbers are a pipeline coverage
snapshot, not a finding that the candidate fabric fits or fails the protocols.

The next compiler milestone is to lower more checked frontend operations into
typed operations, then place and route those operations onto
explicit heterogeneous tiles. Keep exact-target image emission separate from
hypothetical candidate analysis until each candidate has corresponding RTL and
calibration. The upstream [CIRCT FSM dialect](https://circt.llvm.org/docs/Dialects/FSM/)
and [MLIR dialect conversion framework](https://mlir.llvm.org/docs/DialectConversion/)
provide relevant infrastructure; they do not by themselves prove a protocol
mapping or replace the target-specific verifier.
