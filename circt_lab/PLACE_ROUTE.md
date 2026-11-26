# Placement and routing (step 5): design

Status (2026-11-26): R1-R4 and R6 built and tested on today's 5 x 3 grid
(`grid_rrg.py`, `grid_pathfinder.py`, `grid_place.py`,
`grid_chanwidth.py`, each with a `test_*.py`); R5 (latency classes) and R7
(the full flow from `.spt`) remain. The exhaustive `grid_route.py` is the
oracle for R1 and R3; brute force over every binding is R4's.

Findings so far: 26 dead switch edges (tracks into cells without inputs
that cannot continue); PathFinder needs a strong history term on this
small, turn-constrained graph (history 1.0, present growth 1.3; with 0.4
and 1.6 one track stayed shared forever where a routing exists);
bounding-box wirelength alone misses routable placements at 1 track, so
placement falls back to annealing on the router's own overuse; the six
corpus workloads need at most 2 tracks of the contract's 4.

## What the compiler must do

Take a lowered protocol (CIRCT, `spatial.*` ops: lane programs, timers,
shifters, CRC, LUT4 logic, pins) and produce configuration words that run
it at the pins, for any architecture the contract describes. Nothing in
the flow may name a protocol. The steps, VPR's, in order:

1. **Pack**: ops to tile instances (a timer op to a timer, a `reg`
   expression to LUT4s, a coder chain to the coded shifter and CRC). Lane
   cores are clusters: their 64 local lines are a full crossbar with kinds
   (pulse, level), so intra-core routing is selector encoding (the
   existing `CoreEncoder`), never a search.
2. **Place**: tile instances to sites (cells; lanes to lane cores).
3. **Route**: every inter-tile net over the switch mesh.
4. **Time**: cycle latency of every route, folded back into the timing
   parameters that depend on it (below), then checked.
5. **Emit**: configuration words, from the routing result's edges, each of
   which carries the (word, field, value) that turns it on.

## The routing-resource graph (RRG), from the contract only

Nodes:

- `OPIN(cell, k)`: a local output of a cell (`grid.cells[].local_outputs`).
- `TRACK(cell, dir, t)`: the wire leaving `cell` toward `dir` on track `t`
  (`bit_network.directions`, `tracks_per_direction`). Its turn class
  comes from `turn_classes`.
- `IPIN(sink)`: a tile input (`sink_cells`, `sink_words`), whose selector
  picks constant 0/1, a local output or an incoming track
  (`input_selector`).

Edges, each labelled with the configuration it needs:

- `OPIN -> TRACK`: switch selector `local_first..local_last`.
- `TRACK(a, d, t) -> TRACK(b, d', t)` where b is a's neighbour toward d:
  straight (`straight`) or a turn (`turn_first..turn_last`), only where
  the track's class allows the turn (horizontal-then-vertical tracks turn
  once from a horizontal to a vertical direction, never back), never a
  U-turn, never a track change (`track_changes: false`).
- `TRACK(a, d, t) -> IPIN(s)` at the neighbour cell of s: input selector
  `<incoming dir>_first + t`.
- `OPIN -> IPIN` within one cell: input selector `local_first + k`.

A switch-box output (one track leaving a cell) has one selector, so it can
carry one signal: that is the capacity (1) of each TRACK node, and the
reason routing is a negotiation. Turn classes make the class part of the
node: a net that must go west then north takes a vertical-then-horizontal
track only if it can start vertical, so the graph, not the search, encodes
legality, and the router never needs a special case.

The RRG builder is the only code that interprets `bit_network`; a contract
check proves its edge labels against the RTL switch and input selectors
(the RTL repository's `check_grid_contract.py` already proves the selector
values; the new check closes the loop by building the RRG, emitting words
for every edge, and comparing with an RTL simulation of the selector:
milestone R1).

## Routing: PathFinder, with latency as a constraint

Negotiated congestion (McMurchie and Ebeling): every iteration rips up and
reroutes every net; a node's cost is `(b + h) * p` with `b` its base cost,
`h` its history (raised each iteration it is overused) and `p` the present
overuse penalty (raised each iteration, steeply). Nets search with A*
(lookahead: Manhattan distance in cells, exact for this mesh without
segments longer than one). Multi-sink nets grow a tree (each sink routes
from the tree so far). Stop when no node is overused; fail after N
iterations with the overused nodes as the diagnostic.

What this fabric adds, and FPGAs do not have: **cycle latency is
semantics.** A protocol's bit timing is fixed by the tiles (timers), but a
route through registered hops (`hop_cycles`, `registered_links`) delays an
edge by whole clocks, and LANE_TIMING's `5+k` rule already shows the
lane's response time depending on it. So:

- Each sink gets a latency class from lowering: `exact(n)` (the value is
  sampled at a fixed cycle: shift/sample lines, a timer start that a
  later timer edge is measured against), `max(n)` (a response window:
  I2C ACK, SPI next byte), or `free`.
- The router's search state is `(node, cycles so far)` when the graph has
  registered hops; with `hop_cycles: 0` (today) every route has latency 0
  and the state collapses to `node`. For `exact` sinks, paths with the
  wrong latency are not solutions; for `max`, a cost cliff past the bound.
- Where a latency can be compensated in a tile parameter (a timer's
  first count, the resync phase, the core's sample latency), lowering
  marks the parameter `adjust_by(route)` and the compiler folds the
  route's latency into it after routing, then rechecks. This is the
  existing `bit/2 - 2` sample rule made general: the hardware states its
  latencies in the contract, the compiler compensates them, and the pin
  tests prove the result.
- Timing (combinational depth of unregistered hop chains): the contract
  states the longest unregistered chain the clock allows; the router
  treats exceeding it as overuse (so negotiation pushes nets onto
  registered hops) and the timing check reports the worst chain.

## Placement: simulated annealing

VPR's annealer: moves swap two same-type sites (or move to an empty one)
within a range limit that shrinks with the acceptance rate (target 0.44);
temperature from the initial cost spread; exit when the temperature is
small relative to cost per net. Cost: sum over nets of bounding-box
half-perimeter, weighted by fanout (the q(n) correction) and by channel
capacity, plus a timing term for `exact`/`max` sinks once routing has
produced criticalities (a second placement pass, as VPR's timing-driven
mode). Fixed sites: pins, lane cores (a protocol's lane program and its
lane's tiles), memory ports.

Lanes: a protocol takes one lane or two; lane assignment is part of
placement (lane = a site type), with soft tiles placed near their lane's
core (the step-4 floorplan gives each lane core a region; the contract
will say which cells are near which lane).

## Minimum channel width: the co-design loop

Binary search on track count (VPR's `route_chan_width` search): rebuild
the RRG with `tracks_per_direction = W` (the builder takes W as a
parameter; selector widths follow) and route the concurrent workload (step
4.1: five census protocols at once). The smallest W that routes, plus
VPR's customary 30 % margin, sets the hardware's tracks for step 4.2; the
RTL repository's interconnect-scaling measurement prices it. This is the
co-design point: the hardware's track count comes from the compiler's
routing of the real corpus, and the compiler's graph comes from the
hardware's contract.

## Verification (each milestone sturdy before the next)

- **Independent route checker**: from the emitted words alone (not the
  router's data), rebuild connectivity using the selector semantics and
  check every net reaches exactly its sinks, no selector drives two
  nets, latencies match. A router bug cannot pass its own checker.
- **Oracle**: on small layouts, the exhaustive `grid_route.py` and
  PathFinder agree on routability; on random netlists PathFinder never
  claims a route the checker rejects.
- **Property tests**: random netlists at random W: legality always;
  routability monotone in W.
- **Pins**: every compiled protocol passes its RTL pin test and, for the
  submission set, the gate-level test.
- **Contract mutations**: change a selector value or turn class in a
  contract copy; the RRG/RTL check must fail.

## Milestones

| | Milestone | Accept |
|---|---|---|
| R1 | RRG builder from the contract, parameterized by W; edge-label check against RTL selector simulation | every edge's words select the right source in the RTL selector model; contract mutations caught |
| R2 | Route checker from words alone | catches every hand-made illegal route in a fixture set |
| R3 | PathFinder (A*, trees, history), latency-collapsed (hop 0) | agrees with the exhaustive router on all its existing tests; random netlists legal by R2 |
| R4 | Annealing placement | on the existing workloads, routable at the contract's W; wirelength and runtime reported |
| R5 | Latency classes and compensation (`exact`, `max`, `adjust_by`) | the five lane pin designs compile from lowering with compensated timers and pass at the pins |
| R6 | Min channel width sweep over the concurrent corpus | W chosen for step 4.2 with margin; report checked in |
| R7 | Full flow from `.spt`: pack, place, route, time, emit | the six running protocols compile and pass; per-protocol lowerings deleted (step 5.5) |

R1-R4 do not depend on the step-4 floorplan (the RRG is parameterized), so
they can start as soon as step 2 is done; R6 must precede step 4.2.

## Risks

- **Turn classes make some two-pin nets unroutable on a given track set**
  regardless of congestion (a net needing west-then-north with only
  horizontal-first tracks free). PathFinder handles it (other nets move),
  but the min-W sweep must report it separately from congestion.
- **Lane-core lines are not the mesh.** Signals between a lane core and
  soft tiles cross from the core's local set to the mesh at fixed ports
  (mesh0/mesh1 in, outputs out). Few ports: these are the first
  bottleneck; the placer must treat them as fixed pins of the lane site.
- **Latency exactness and negotiation interact**: forcing an `exact`
  sink's latency can make a net unroutable while plain negotiation would
  succeed. Compensation (`adjust_by`) is the preferred answer; the router
  reports which sinks lacked it.

## R7 decision: pin-referenced source timing

`.spt` timer counts are raw hardware counts tuned to one chip: the UART
example's transmit timer is `first: BIT - 2 cycles` "to cover the pulse and
pin registers", and its receiver's `BIT / 2 - 1` is "net of the input
synchronizer". A source written that way is correct only on the chip whose
latencies it absorbed, so it cannot pass the held-out check of the finish
line (protocols written from specifications compile with zero edits).

**Decision:** source times are **pin-referenced** — `first` is the time
from the starting event at the pins to the action at the pins (`sample:
rx at rx_bit.tick` samples the pin then) — and the compiler subtracts the
target's latencies, which the contract states and RTL tests measure
(`lane_core.edge_to_sample_cycles` is the first, R5; the output path's
`5 + k` of LANE_TIMING.md is next). Existing examples drop their hand
compensation (`first: BIT / 2` for mid-bit receive). Cost: every example's
timing constants change by 1-2 clocks, and the older backends (the
`arch/spatial` actor fabric) must apply their own latencies the same way,
or keep a legacy flag until they are retired.

**Implemented (2026-11-26).** Reference points, by the class of path
between a timer's start and its tick's effect:

| start | tick's effect | taken off | where stated |
|---|---|---|---|
| input pin edge | samples that pin into a shifter | old chip 1, lane core `edge_to_sample_cycles` (1) | `hardware.rs` `PIN_EDGE_TO_SAMPLE`; contract, measured by `sample_latency_matches_contract` |
| shifter busy | shifts that shifter onto a pin | old chip 2, lane core `start_to_shift_cycles` (2) | `hardware.rs` `SHIFT_START_TO_SHIFT`; contract, measured by `start_to_shift_matches_contract` |
| anything internal at either end | | 0: the count as written | |

A `first` no larger than the latency is refused ("measured at the pins").
The examples were migrated (`BIT - 2` -> `BIT`, `BIT / 2 - 1` -> `BIT / 2`,
and the same on every other pin-started timer); all 35 example images are
byte-identical to before, so no compensation is applied twice. Tests:
`tests/pin_timing.rs` (both boundaries, the internal case, one pin cycle
moves the encoded count by one) and `test_grid_lower.py` (the lowering
emits `sample_first`; its image equals the hand-offset count's; a time
inside the offset is refused). The lane core's start-to-shift latency
is measured (2 clocks for first 3/6/9, the next bit exactly the period)
and stated in the contract; `grid_lower.py` lowers a transmit timer to
`shift_first` and a host stream to a pull/shift program.
`lanes/uart_tx_8n1.spt`, written from the specification (`first: BIT`),
lowers to the data the RTL pin test `uart_tx_lowered_from_source` runs:
every bit, the start bit included, is exactly 64 clocks at uio 6, and the
same data built with a latency of 1 fails it (start bit 65 clocks).

## R7 coverage (2026-11-26): what lowers to one lane core, and why not

`grid_lower.py` over all 35 examples: 2 lower (`ps2`, at the pins;
`byte_tag_errors`). Refusals, each a named construct, grouped by what
would remove it:

| Needed | Examples | Where it comes from |
|---|---|---|
| a second coded shifter (two lanes) | uart, i2c, jtag, loopback, byte_filter, byte_framing, byte_xor, io_saturation, 4 memory_dual/unequal | steps 2.5-2.6 (hardware) |
| `timer.run` (level-gated timers) | 5 protocol_control variants | lowering rule: hold mode with aux = !run |
| counters | spi, usb, ethernet_tx | timer count mode ("tick after N events") or controller LOOP |
| CRC resource | crc_link_trace, ethernet_tx_coded (+ usb, ethernet_tx) | the core's CRC tile: polynomial/additive, link |
| registers / logic in signal paths | contexts_logic, packets (+ spi) | step 5.3: reg expressions to LUT4 cells |
| host-to-pin streams | contexts, contexts_initial_one | auto-pull transmit program |
| a third timer | byte_tag_gap | two lanes, or a counter rule |
| no receive stream (host-driven only) | 3 memory_bandwidth, stored_bytes | exported functions to controller programs |
| `shifter.end`, raw shifter (no frame) | packet_errors, swd | burst/end rules; bit-level shifter use |

The largest group waits on hardware (a second lane); the rest are
compiler rules, each with a pin test to land it like PS/2.
