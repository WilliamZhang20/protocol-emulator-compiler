# Architecture laboratory

The laboratory compares explicitly specified hypotheses. Its outputs are models,
not proofs of protocol correctness, RTL performance, physical timing, or area.
Keep the workload, compiler revision, hardware revision, candidate parameters,
and simulator/physical-flow versions with every result.

## Extract the configured graph

```sh
cargo run -- model-graph examples/uart.spt > /tmp/uart-graph.json
```

The `spatial.partial-architecture-graph.v1` JSON is extracted from the same
compiler allocation and register writes as the image. It records physical
resource indices, selectors, direct/byte links, context overrides, and unknown
producers. `graph_complete` is false: omitted constants, memory traffic,
activities and dependencies cannot be treated as absent. Context overrides
are alternatives; base and override edges do not necessarily operate together.
The hardware revision names the committed baseline. Pending local RTL and the
actor ABI may differ and must be checked independently.

The `check` command reports abstract placement and link-width hypotheses. A
byte link conservatively costs eleven forward lanes (eight data, end, error,
valid) and one reverse ready lane. A configured shifter/CRC link costs three
forward lanes and one CRC-return lane. All are counted even if a particular
mode uses fewer. This is a conservative interface proxy: it does not model
router control, timing, actual physical wires, or which endpoints need every
signal. The width sweep unions alternate contexts, conservatively provisioning
all possible configured connections. Its historical numbers calculated with
8-bit byte links and one-bit CRC links are no longer comparable.

## Evaluate a memory hypothesis

```sh
cargo run -- model-memory tests/fixtures/memory_experiment.json
```

Each request describes a byte address, access size, earliest release, explicit
predecessors, and optional absolute completion deadline. Each candidate sets
capacity, bank count, stripe size, ports, latency, and port initiation
interval. Bank mapping is `(address / stripe_bytes) % banks`; every request
must fit within one stripe and capacity. Read and write access types share the
same port and timing. Releases and dependencies are the workload contract,
not inferred from the current controller. A dependency delays a request until
its predecessor completes. Among ready requests, the lowest release cycle then
lexicographically smallest ID wins; each bank picks its earliest available
port. Cycles denote service start and completion boundaries; a request
completes at `start + latency`. A deadline is met if completion is at or before
it. Reports include each request's ready, start, completion, bank and deadline,
as well as aggregate queue cycles and misses. Requests with no deadline do not
contribute to deadline misses.

The model is deterministic and validates references, cycles, address/clock
overflow, and unsupported cross-stripe requests. It does not model data values,
coherence, request generation, instruction timing, finite queues, backpressure,
interconnect contention, read/write differences, placement, or PPA. The input
must state the causal ordering of dependent accesses, including same-address
hazards. Observed RTL issue times are not automatically counterfactual releases:
feeding them into this model without reconstructing dependencies can hide or
invent bottlenecks. A faster candidate is conditional on these assumptions.
Compare RTL pin-level correctness and measured resource requests before using a
candidate to justify a hardware change.

## Remaining work

A complete laboratory requires an RTL-derived hardware manifest; complete graph
extraction with effective contexts, typed widths, latency, and valid/ready
semantics; causal trace generation and calibration; a legality-preserving
resource/route mapper; workload and timing contracts; and synthesis/physical
measurements. It also needs a general width-aware protocol IR and functional
simulation of proposed transformations. Current reports do not implement those
features and must not be used as proof that an arbitrary protocol fits.

## Stored-transfer IR and program memory

The compiler now lowers constant `.send` operations through a typed
`StoredTransfer` representation before PX8 emission. It contains the logical
FIFO streams, the selected SRAM table, and repeated phases of tagged FIFO
writes. For two streams it interleaves bytes until the shorter stream ends,
then drains the remainder, tagging each stream's actual last byte. The
controller still services FIFOs sequentially: a blocked FIFO stalls its peer,
and the IR does not promise independent progress or a deadline.

`model-graph` includes `program_memory`: exact code length and total capacity,
configuration and constant-data object offsets and sizes, table reference
counts, and each stored-transfer schedule and table address. These are derived
from the assembler that builds the image, including constant-table interning;
configuration and data objects can be interleaved. The source language's
`parallel` block accepts unequal nonempty stored streams. The backend supports
at most two hardware TX channels, and the code, configuration, and data share
the same 1 KiB SRAM. Stored sends use the single PX8 counter and data pointer;
they still cannot be nested in a runtime-counted loop.

This is a real but narrow IR boundary: it describes stored stream operations,
not the semantics of all protocol, control, timing, or arbitrary-width data
operations. The parser and hardware-bound elaborator remain the source of
those other operations. A general, target-independent protocol IR and alternative
memory backend are not implemented by this change.

## Logical protocol IR and control-tile candidates

```sh
cargo run -- model-ir examples/protocol_control_two_input.spt
cargo run -- model-protocol examples/protocol_control_two_input.spt \
  --max-inputs 2 --max-states 4 --max-action 3 \
  --response-cycles 1 --response-deadline 2
```

The `spatial.logical-program-ir.v1` representation records ordered source
resources, expressions, functions and control regions, stream stages, context
overrides, and protocol state machines before physical allocation. Its
`requirements` report counts declared resources, identifies base and context
shifter widths, and flags possible runtime frame updates. Static shifter widths
are evaluated without the current target's 16-bit limit; dynamic or unresolved
widths preserve their expressions. CRC state widths are recorded without the
current hardware limit. Host streams carry bytes, which is recorded separately
from shifter payload width. Explicit timer `every` and `first` durations are
exact rational cycle counts; runtime updates, holds, and resynchronization can
change observed timing.

The `connections` report preserves source input expressions, referenced names,
stream endpoints and stages, and context overrides. It distinguishes timer
start/stop and shifter sample data/trigger roles. A source
expression can mention multiple inputs, and a syntactic dependency does not
imply a dedicated physical route. Implicit paths and runtime changes are not
included.

Each reference carries a `signal_kind` (`level`, `pulse`, `not_signal`, or
`unknown` for unresolved/ambiguous names), and each input carries a `check`
against the elaborator's contract for its role: which outputs exist, which
roles need a level (constants are encoded as pulses, so they are not levels),
which need a pulse or constant, that pins copy a single level or constant,
that `hold` is `level` or `!level`, that `streak` is `ones`/`zeros`/`equal`
of a level, that `link` names a shifter, and that a `reg` formula uses only
logic operators over signals. Its `status` is `valid`, `invalid` (the
elaborator rejects this input), or `unknown` (an unresolved name). A valid
`reg` formula reports its distinct pulse and level source counts; target
logic-cell limits, such as the current four-input/two-pulse cell, are not
applied, so candidates with other cell shapes can be compared. A differential
test (`tests/logical_graph.rs`) checks that the logical check and `compile`
accept and reject the same single-input variants, and that every input in the
example corpus is valid. A valid check is not a placement or routing proof:
resource counts, selector reachability, protocol action availability in the
selected implementation, and cross-field rules (such as a linked CRC omitting
`data`) are still checked only at allocation.

The protocol state-machine portion records logical input names, explicit
transitions, action IDs, and the implicit hold/no-action behavior of missing
rows. The existing actor/controller mapper consumes those state machines. The `LogicalProtocol::step` interpreter
provides a reference for sequences of input values. Its input is a sampled
clock-cycle trace; it does not simulate timers, pin synchronization, FIFO
backpressure or interaction between protocol machines.

`model-ir` extracts the logical program even when the current chip lacks
resources for it. This extraction evaluates the clock and constants and checks
signal names and pulse/level roles in the `connections` report (above), but it
does not reject a program for an invalid input; read the check status. Expressions and control regions
are preserved structurally; they are not yet a cycle-accurate execution model.
A successful extraction is not a compile success. `model-protocol` computes
reachable states and Mealy-machine equivalence classes, then checks the number
of inputs, minimized states, reachable action IDs and an optional response
latency against a candidate. Timing is unknown if either the candidate latency
or deadline is absent. Each machine is assessed independently: routing,
protocol interactions, throughput, area and physical timing remain unmodeled.
All Boolean input combinations are considered, including combinations that
could be impossible when inputs are constants, aliases, or correlated. Thus
the computed state requirement can be conservative for such inputs.

The program IR preserves most source constructs, but is not yet a complete
semantic or executable program IR. In particular, timer, shifter and CRC
behavior, function calls, memory operations, and context switching still
need resolved types, semantic lowering, and independent models. The
stored-transfer IR described above is currently lowered after physical FIFO
allocation and is not yet the general memory IR.
