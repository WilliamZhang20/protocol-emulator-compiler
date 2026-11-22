# Initial spatial architecture audit

## Purpose and baseline

This is the first evidence pass for the architecture objective:

> Increase independent protocol work that overlaps at the required latency,
> keep producer/consumer communication local, and provide enough memory
> bandwidth to feed that work.

Correctness, protocol coverage, available silicon area, and timing closure are
constraints. The three objectives are not collapsed into a single score.

Audited checkouts:

- Hardware: `arch/spatial8x4`, commit `7dd2f41`.
- Compiler: `spatial-8x4`, commit `8714ade`.
- Hardware GDS CI: current run `44337729589`; pre-actor parent run
  `43523858557` at `be33e80`.
- Compiler CI: run `44412022045`, success.

The CI GDS artifacts were downloaded one at a time, summarized here, and
removed after inspection. No commit or push was made.

## Findings

### 1. “8×4” is a Tiny Tapeout allocation, not a compute mesh

The project reserves 32 Tiny Tapeout tiles. Inside that region, the RTL
instantiates resource arrays and a shared 64-line signal space. It does not
define a corresponding 8-column by 4-row placement of timers, shifters, logic,
actors, or byte stages. The SRAM macro has a fixed floorplan coordinate;
standard-cell placement is handled by the physical flow.

The logical wiring rule gives each signal one registered hop. It does not
bound physical wire length. Routed CI has functional nets near 1.8 mm, and
named examples include `core.s_rx_data_0[13]` at about 0.892 mm and
`core.counter[0].tile.lines[3]` at about 0.584 mm. The longest net overall is
the clock at about 1.815 mm. The shared signal network therefore needs
physical locality measurements before we describe it as a local fabric.

### 2. Routed cost of the first protocol-actor implementation

The two GDS runs use the same 8×4 floorplan configuration, clock target, and
signoff flow. Comparing the actor commit with its immediate pre-actor parent:

| Measure | Before actors | Current actors | Change |
| --- | ---: | ---: | ---: |
| Detailed-route wire length | 2.200 mm | 2.300 mm | +4.6% |
| Longest routed net | 1.515 mm | 1.815 mm | +19.8% |
| Placed instance count | 52,045 | 53,989 | +3.7% |
| Standard-cell area | 707,092 µm² | 727,885 µm² | +2.9% |
| Slow-corner setup slack | 3.924 ns | 4.295 ns | +0.371 ns |
| Detailed-route DRC errors | 0 | 0 | unchanged |

The current STA summary also lists 2 max-cap and 13 max-slew violations; the
prior summary listed 0 and 1. Tiny Tapeout's GDS prechecks passed, but these
electrical warnings need attribution before the next physical comparison.

This is a useful cost baseline, not evidence that actors should be removed.
Each current actor can select any one of 64 shared lines. The actor therefore
adds a programmable global input selection path, and neither RTL nor mapper
currently constrains physical placement around that selection.

### 3. Compiler search exists, but its spatial-control target is narrow

The compiler minimizes unreachable/equivalent protocol states and evaluates
actor assignments against a PX8 controller candidate when eligible. Current
actor capacity is four states, one selected binary signal-space input, and
three action outputs; the hardware has two actors. The controller fallback is
currently limited to one graph with no exported functions, a timer tick pulse,
hold-only low rows, and at most two actions. Its modeled service time is ten
cycles, and it requires at least sixteen cycles between input pulses. A
spatial actor responds in one registered cycle.

The examples confirm the cost difference on the same kind of protocol graph:

- `protocol_control.spt`: a five-state source graph minimizes to two states
  and maps to an actor in one cycle.
- `protocol_control_controller.spt`: a five-state graph does not fit either
  actor; the eligible controller fallback is selected at ten service cycles.

These are mapper exercises, not a migrated real protocol. In the representative
I2C, USB, and coded Ethernet examples, the control events are still owned by
the controller. The current protocol graph language describes a single
binary input and transition/action table; it does not yet extract such graphs
from the examples' multi-input, byte-dependent control flow. The compiler's
current placement score also counts actor candidates and channel-local
shifter/action pairs, but it does not use routed cell coordinates, physical
wire delay, memory-port conflicts, or measured parallel activity.

Current static resource checks are still useful inputs: I2C is 514/1024 image
bytes, USB 247/1024, coded Ethernet 363/1024, stored-bytes 143/1024, and UART
60/1024. These are program-image sizes, not SRAM bandwidth or conflict
measurements.

### 4. The SRAM is a shared service point, but banking is not yet justified

The design uses one foundry 1024×8 single-port SRAM macro, about 49,419 µm² in
the current physical report (about 4.1% of the 1,208,170 µm² core). Instructions,
configuration tables, and stored data share it. Controller instruction fetch,
configuration loads, and data reads cannot use independent ports. The compiler
currently reports image capacity but not dynamic reads, request overlap,
or bytes delivered per protocol cycle.

Banking could raise bandwidth if independent actors issue concurrent accesses
and the compiler places those accesses to avoid conflicts. It will not improve
execution if one PX8 PC remains the only requester. Also, the available macro
is a 1K×8 instance: making multiple physical banks from this macro would
replicate capacity and macro area. A smaller bank macro is not present in the
current design configuration. First model banks and conflicts in the compiler;
do not change RTL storage until the model shows a workload bottleneck and the
PDK offers a feasible physical implementation.

## Direction and first experiment matrix

| Experiment | Compiler work | Evidence to compare | Decision it informs |
| --- | --- | --- | --- |
| Control mapping | Compare central PX8, current actor, and expanded actor variants on the same graph/workload | Event-to-action cycles, PX8 active/stall cycles, independent actor overlap, correctness, area and routed timing | Which control functions merit spatial actors, and what actor capacity is needed |
| Channel locality | Add channel/stage regions and make local-line connections cheaper than cross-region links; preserve legal global routes | Routed net length/delay by producer-consumer pair, fanout, critical paths, transaction latency | Whether to split the shared 64-line network into local regions plus sparse global links |
| SRAM contention model | Record instruction/config/data accesses and simulate 1, 2, and 4 single-port banks with compiler-controlled placement | Accesses/cycle, conflicts, stalls, throughput, image capacity, expected macro area | Whether banking has a real benefit before any RTL/macro change |
| Real protocol mapping | Choose one control-heavy example and represent its actual multi-input/byte/event transitions; keep datapath and host boundaries explicit | Same protocol vectors, response latency and jitter, actor overlap, PX8 retirement, area/timing | Whether the actor model generalizes beyond synthetic FSMs |

The first practical workloads should include independent concurrent channels
(for example, two UART streams), a control-heavy case (I2C or USB), and a
stored-data stream. Compare all candidates with the same workload and input
trace. The compiler should produce both a candidate mapping and an explanation
of its resource, bank, locality, parallelism, and cycle costs. Physical runs
then calibrate the compiler's locality and area estimates.

## Recommended order

1. Add dynamic execution and memory-traffic counters to compiler simulation;
   static image size alone cannot support a banking decision.
2. Extend the placement report with explicit producer-consumer edges, selected
   signal lines, fanout, and channel/stage regions. Keep the current shared
   network as the baseline candidate.
3. Map one real control-heavy protocol fragment to a spatial actor and compare
   it with the existing PX8 path. Expand state/input/action capacity only in
   response to that mapping's concrete failure modes.
4. Feed routed net lengths, path delay, area, and warning attribution back into
   compiler candidate costs. Revisit interconnect and memory banking after this
   calibration.

Do not optimize for maximum tile utilization as a standalone objective. Prefer
independent work that overlaps at the required protocol latency and remains
within physical limits, while reducing remote communication and memory stalls.

## Compiler and structural topology experiment (2026-11-19)

### Workload evidence

The compiler was run against UART, I2C, USB, and coded Ethernet sources. The
resource map shows why a strict channel-0/channel-1 physical partition is too
coarse as the only placement rule:

- UART uses shifter/timer pairs on both channels, but the edge detector and
  pins are shared resources allocated independently of those pairs.
- I2C uses both shifters as separate SDA and condition-pattern stages, with
  shared logic, timers, event handling, and pin resources. Those roles do not
  align with two identical byte-stream channels.
- Coded Ethernet uses one shifter, one CRC, four timers, and three logic cells;
  all timing and control are concentrated around one active stream.
- USB uses one shifter, one timer, one CRC, both counters, and all eight logic
  cells. Its useful groups are protocol stages around one stream, not two
  balanced channels.

The optional compiler RTL profiler was stale: it expected `core.cpu`, while
the current RTL has `core.ctrl`. It now reads the controller state and
instruction from the RTL-only `ctrl` instance. With `SPATIAL_PROFILE_RTL=1`,
these workloads passed and reported:

| Workload/test | Cycles | Controller event wait | FIFO wait | Shifter active | Max concurrent shifters |
| --- | ---: | ---: | ---: | ---: | ---: |
| UART loopback | 1,978 | 0% | 0% | 96% | 2 |
| UART terminal | 2,636 | 0% | 0% | 94% | 1 |
| I2C reads a register | 25,536 | 97% | 0% | 97% | 2 |
| I2C absent device | 26,976 | 97% | 0% | 97% | 2 |
| I2C with clock stretching | 26,368 | 97% | 0% | 97% | 2 |
| USB packet | 3,152 | 90% | 0% | 71% | 1 |
| USB back-to-back packets | 5,232 | 91% | 1% | 66% | 1 |

These are RTL simulation cycle counts. They show shifter activity overlapping
long controller waits, but do not measure per-tile activity or routed locality.
The coded Ethernet example's simulation has a long virtual schedule, so it was
not included in this bounded profile set.

### Synthesized connectivity evidence

Yosys 0.67 synthesized the current top and emitted a hierarchical JSON netlist.
The structural report contains 31,338 generic cells after flattening (including
the SRAM black box; this is not a mapped area estimate). Before flattening,
28 resource instances expose a 64-bit selector input: 8 byte tiles, 8 logic
cells, 4 timers, 2 counters, 2 shifters, 2 CRCs, and 2 control actors. The
first twenty receive the shared core `lines` bus directly; the eight byte
tiles receive its delayed `tile_lines` copy. This is evidence of a broad
programmable signal network, not evidence that 28 distinct bus copies will
route as 28 physically independent networks.

This also explains why merely grouping blocks by channel may not improve
locality: runtime-programmable line selection permits any block to select any
line, so the RTL still presents a wide selector interface across the fabric.
The compiler currently reports a workload's resource and signal-line outputs,
but it does not yet know each tile's selected input-line set, signal activity,
or physical coordinate. A compiler estimate that assigns spatial locality
before these are available would be an assumption, not a measurement.

### Topology direction to carry into placement experiments

Keep two channel regions as an outer organization for the existing shifter,
byte-stage, and FIFO ownership. Split each active channel into smaller
adjacent groups based on its actual dataflow:

1. pin/edge capture at the I/O boundary;
2. bit timing and coding around the channel's shifter, paired CRC, and the
   timer resources the compiler assigns to that workload;
3. the four byte stages and their channel FIFO;
4. protocol control near the events and actions it uses most.

Keep shared logic, counters, controller/SRAM service, and low-frequency
cross-channel links in a shared region. Preserve global routes where a real
workload needs them. This is a candidate hierarchy, not a proven floorplan;
I2C and USB already show that protocol stage relationships can cross channel
boundaries.

The evidence is sufficient to reject a single channel split as the complete
locality model, but not sufficient to choose final groups or change the RTL.
The next compiler deliverable should record each configured input selector,
producer-consumer edge, and observed activity per test. Then compare the
two-channel and staged partitions against that workload graph. Only after the
compiler identifies a stable mapping should placement guidance be tested.

### Physical-flow availability

The local environment has Yosys and Verilator but no OpenROAD, LibreLane,
container runtime, or installed IHP PDK. Thus no local placement/global-route/
detailed-route comparison could be run for candidate RTL. The existing CI GDS
numbers above remain the only routed baseline. Reordering RTL instances or
adding hierarchy attributes without a router would produce no credible
routing result, so no such candidate was labeled an experiment result.

### Follow-up physical and compiler experiment

The later isolated hardware experiment supersedes the physical-flow status
above. Its report, `docs/8x4-spatial-locality-experiment.md` in the
`/tmp/protocol-emulator-8x4-route` worktree, records hard-cluster placement
regressions, inert channel GUIDE/FENCE regions, and a byte-line RTL candidate.
That candidate passed all 134 RTL Cocotb tests, but global-route results were
mixed and its standalone detailed route stopped at 90% after 21,640
intermediate violations without final metrics. It is not evidence for adopting
the RTL change.

The compiler now exports a static named signal-selector graph and reports
abstract channel, type-based stage, and mesh partitions. Across UART, I2C, USB,
and coded Ethernet, the fixed type-based stage partition had more estimated
cross-cluster edges than the index-parity channel heuristic. These are
unweighted structural estimates, not dynamic or physical measurements. The
result argues for compiler search over graph-based assignments and against
encoding fixed cluster placement in RTL before dynamic activity and physical
calibration exist.
