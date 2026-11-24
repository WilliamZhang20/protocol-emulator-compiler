# Grid topology experiment

This experiment compares configurable **bit routing**, not complete protocol
implementations. `grid_route.py --emit-mlir` builds a verified
`spatial.grid_route_problem`; `spatial-circt-map --route-grid` searches actual
per-track output selectors, fixed turn classes, fanout and driver conflicts.
Each source-to-sink path retains one track and uses the Manhattan path allowed
by its turn class. It explores tracks for each sink and backtracks across nets.
An exhausted search returns `unknown_search_limit`; an exhausted choice set
means `unroutable_in_model`, not proof that a different tile assignment,
temporal schedule, logic remapping or relay implementation cannot work.

The current-grid SPI/UART component routes are encoded into config words,
checked independently against the contract, and exercised in the RTL fabric
testbench. `test_center_candidate.sh` repeats this for generated RTL that
swaps timer 0 with the LUT. This does not validate full source programs.
The [center-timer physical branch](https://github.com/WilliamZhang20/protocol-emulator/tree/experiment/grid-centre-timer-physical)
also retargets the baseline black-box pin fixtures and runs physical CI.

## Workloads and scope

- SPI is the source-checked 12-bit memory-backed component, UART is the
  source-checked concurrent TX/RX component. Both inherit fixed host/frame
  scheduling and placement rather than full source-function lowering.
- JTAG checks 11 direct source connections. I2C checks 5 of 18, USB 2 of 23,
  and Ethernet 5 of 34 source connections. These are deliberately incomplete
  direct-bit slices; missing expressions, state, sequencing and streams can
  substantially change the needed hardware and routing.
- All 35 repository `.spt` examples were exported through the Rust frontend
  and CIRCT source-evidence path. An additional automatic first-declared-tile
  binding routes direct bit-reference slices for 32/35 programs; the other
  three have no supported direct edges. At the current placement, the bounded
  model routes all 32 slices with three or four balanced tracks and 12 with
  two; 20 fail under two tracks. These are separate incomplete slices with
  omitted edges and fixed bindings, **not** complete programs or fit proofs.
  [Per-program inventory and results](reports/grid_auto_corpus_summary.json)
  record the omissions and candidate outcomes.

See [the machine-readable summary](reports/grid_step3_summary.json) for the
candidate matrix and source hashes. The current four-track grid routes all six
selected slices; the balanced two-track hypothesis does not route the selected
JTAG slice in this model. Three tracks route these slices, but that is not a
minimum width for complete protocols or a reason to remove a current track.

The evidence separates the gaps as follows:

| Observation | What it establishes | What remains open |
| --- | --- | --- |
| A source expression, context or function is omitted from a slice | The present extraction/lowering did not represent it. | Whether existing tiles can implement it, including temporal reuse. |
| A fixed-placement bit slice fails the bounded router | That particular placement and modeled track configuration cannot route under the searched rules. | Other placements, logic decomposition, or time sharing; a fundamental hardware gap is not established. |
| A candidate image passes RTL and gate tests | The tested pin-level behaviors work on that candidate. | Untested source functions and complete protocol compliance. |
| A candidate passes physical CI | That implementation met the workflow's physical gates for that run. | The reported electrical-limit violations and other candidates' physical feasibility. |

In particular, the USB and Ethernet direct slices do not establish that the
current grid can run their line coding, packet control, or complete frame
streams. The reused CRC and coder component tests are supporting evidence,
not substitutes for full protocol lowering and pin-level tests.

The center-timer candidate reduces maximum hops in the SPI/UART slices from
four to three and in the selected I2C/JTAG slices from three to two. The direct
neighbor-link hypothesis reduces some occupied track segments further, but
requires extra configurable input muxes and has no measured implementation.
Registering all current hops adds up to four cycles in the measured slices;
the selected central links add up to two. These are arithmetic hop counts,
not verified waveforms, compensated schedules or STA results.

The IHP SG13G2 proxy maps one fully connected four-port switch to approximately
682, 1,364, 2,046 and 2,728 µm² at 2, 4, 6 and 8 tracks respectively;
registered-output proxies cost approximately 1,045, 2,104, 3,142 and
4,187 µm². The four-track combinational proxy is SAT-proved equivalent to the
actual switch. These are **isolated switch cells**, excluding configuration,
boundary optimizations, clock tree, interconnect and SRAM. A full top synthesis
using the same proxy library measured the baseline at 151,322 µm² of standard
cells and the center RTL at 152,517 µm² (+0.79%), excluding both SRAMs.

The baseline and center-timer RTL also ran the same Tiny Tapeout CMOS5L
physical flow. These are one run each, including physical optimization:

| Post-route metric | Baseline `9175808` | Center `82a420d` |
| --- | ---: | ---: |
| Standard-cell area, µm² | 233,185 | 236,505 |
| Two SRAM macros, µm² | 98,838.5 | 98,838.5 |
| Worst setup slack at 20 ns, ns | +3.875 | +2.487 |
| Worst hold slack, ns | +0.203 | +0.101 |
| Max slew / cap / fanout violations | 1 / 1 / 130 | 4 / 0 / 128 |
| KLayout DRC errors | 0 | 0 |

Both physical workflows passed GDS, Tiny Tapeout precheck, and gate-level tests.
The center's first CI used baseline hand-routed fixtures and failed at the
relocated LUT; its corrected branch retargets the same pin-level fixtures and
passed the complete test. Physical pass is not complete electrical signoff:
the reported slew, capacitance and fanout counts remain open observations.

**Decision:** keep the current placement as the baseline. The center candidate
has fewer modeled hops but this physical run increased standard-cell area by
1.42%, reduced setup slack by 1.39 ns, reduced hold slack by 0.10 ns, and has
more reported slew violations. This is not a universal result: the grid's
logical coordinates do not constrain physical cell placement, and one run
per candidate is insufficient to characterize seed or placement variability.
Revisit the center only with a specific measured benefit and comparable full
protocol workloads. Do not add tracks, direct links or registered hops solely
on these partial-slice results.

The raw [baseline](../../protocol-emulator/experiments/grid_fabric/reports/baseline_9175808.json)
and [center](../../protocol-emulator/experiments/grid_fabric/reports/centre_timer_82a420d.json)
metric snapshots identify their CI runs. They are distinct from the isolated
switch proxies above.

## Reproduce

With the sibling hardware checkout, Rust frontend and built CIRCT mapper:

```sh
# In protocol-emulator: produce the isolated-switch proxy report.
python3 experiments/grid_fabric/measure_switch_candidates.py \
  --liberty /path/to/sg13g2_stdcell_typ_1p20V_25C.lib --yosys /path/to/yosys \
  --output /tmp/switch-area.json

# In protocol-emulator-compiler: source evidence, topology sweep, RTL candidate.
python3 circt_lab/investigate_workloads.py --frontend target/debug/spatial \
  --mapper "$CIRCT_MAP" examples/*.spt > /tmp/source-corpus.json
python3 circt_lab/grid_auto_corpus.py --evidence /tmp/source-corpus.json \
  --mapper "$CIRCT_MAP" --output /tmp/grid-auto-corpus.json
python3 circt_lab/grid_experiment.py --frontend target/debug/spatial \
  --mapper "$CIRCT_MAP" --area /tmp/switch-area.json \
  --corpus-evidence /tmp/source-corpus.json --output /tmp/grid-sweep.json \
  --summary /tmp/grid-sweep-summary.json
CIRCT_MAP="$CIRCT_MAP" CIRCT_OPT="$CIRCT_OPT" SPATIAL_DIALECT="$SPATIAL_DIALECT" \
  circt_lab/test_center_candidate.sh
```

The direct-link and registered-hop variations are model-only; only the
four-track center-timer variant has candidate RTL and a physical-flow run.
