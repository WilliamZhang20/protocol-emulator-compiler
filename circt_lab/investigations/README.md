# Protocol, routing and memory investigation

These are decision probes, not a full compiler or measured architecture PPA.
All three programs below are reproducible against the checked-out frontend and
mapper. `test_workload_evidence.py` checks source mutations and both routing
hypotheses; `test.sh` also checks the memory results.

## I2C and JTAG source connectivity

```sh
python3 circt_lab/investigate_workloads.py --frontend target/debug/spatial \
  --mapper /tmp/spatial-circt-map examples/i2c.spt examples/jtag.spt > /tmp/workload-evidence.json
```

The extractor calls the Rust frontend and the CIRCT tool's verified
`--frontend-json` interface. It preserves source expressions, function bodies,
contexts, resolved references, resource fields and known 1-bit signal types.
Word widths and behavior not known at this boundary remain unknown. It is an
investigation over source structure, not semantic lowering of functions,
transaction execution, or a proof of simultaneous activity.

JTAG has 11 valid base-context connections. Both source shifters directly use
`go` as their start source and `clock.fall` as their shift source. A combined
block with a common event input is therefore plausible *if* it provides the
two data states and distinct TMS/TDI output paths required by the source.
Two separate shifters can also supply those paths; neither option is placed,
implemented, or physically compared here.

I2C has 18 valid base-context connections, two open-drain pin declarations,
and explicit `timer.hold` dependencies on SCL or the `stretched` register.
Its two declared shifters do **not** have the same direct shift source:
`sda_bits` uses `sda_step`, while `pattern` uses `cond_clock.tick`. A
combined block with independent event inputs is one hypothesis; a single
shared event input is not established without proving context-specific
relationships. Function bodies include runtime `set` operations and the
clock-stretch sequencing. They are retained, not lowered, so the difference is
not by itself a proof that any particular block cannot implement I2C.

## Selector and event timing probes

The two `jtag-*-routes.mlir` files encode the same **source-derived Boolean
input demands** against hypothetical selectors. In `shared`, TMS and TDI
must both use one static selector whose sources are `mode.out` and `data.out`;
the existing exact selector search rejects that graph. In `separate`, each
gets a lane and the search succeeds. Both shift inputs share a single relay
for the same `clock.fall` source, demonstrating valid same-signal fanout in
this abstract selector model. The successful clock paths use two registered
selector stages. These hypotheses do not include actual RTL pin endpoints,
word streams, fanout limits or a full protocol timing proof.

`route_timing_probe.py` reads the actual source timer periods and compares
hypothetical registered pipelines. A pipeline that accepts an event every
cycle preserves the Ethernet example's two-cycle input spacing at one, two,
or three stages, even though absolute latency rises. Throughput alone does
not establish feedback timing, output phase, pin timing, or selector legality.
For I2C it calculates a two-flop input plus one route: a stable level sampled
at edge 0 reaches a downstream consumer at edge 3. Asynchronous phase adds
uncertainty. This is not a proof of valid clock stretching.

The hardware-side `make tile-route-latency-probe` independently simulates
existing `proto_route` and `proto_pin_in` primitives: three route stages
preserve a pulse every two cycles, and a two-flop input followed by one route
is observed by a synchronous consumer at edge 3. This RTL micro-test has no
Ethernet encoder or I2C bus and does not prove either protocol works.

## Memory counterfactual

```sh
python3 circt_lab/investigations/compare_memory.py --frontend target/debug/spatial
```

This uses the existing Rust memory scheduler with explicit synthetic requests:
16 preloaded UART-source bytes consumed every 640 cycles, plus a **different**
background host object requested every 26 cycles at a phase chosen to collide
with two actor reads. The shared case has one 1 KiB bank; the split case has two 1 KiB banks.
The same logical background object is placed at address 512 or 1024 respectively,
so it shares the actor bank in the first case and uses the other bank in the
second. Both alternatives have one-cycle service and initiation, and
the model uses deterministic host-first tie breaking, not the RTL's round-robin
policy. The queue candidate starts empty and requests up to four bytes ahead;
its fixed request schedule is checked against a four-byte occupancy bound.

| Model | Queue | Actor deadline misses | Actor port-wait cycles |
| --- | ---: | ---: | ---: |
| Shared 1 KiB bank | none | 2 | 2 |
| Two 1 KiB banks, objects separated | none | 0 | 0 |
| Shared 1 KiB bank | 4 bytes | 0 | 7 |
| Two 1 KiB banks, objects separated | 4 bytes | 0 | 6 |

Prefetch moves requests earlier; extra wait cycles are not an overall speed
measurement. This comparison demonstrates that under *these assumptions*
separate banks and buffering are two ways to avoid the chosen short deadlines.
The experiment also sweeps all 26 host arrival phases: the unbuffered
one-bank case misses at least one actor deadline in 13 phases (worst two
misses); the other three candidates have no misses in this finite trace.
The actual coded-Ethernet example declares a 60-byte stored frame and a
68-byte on-wire prefix/frame before its separately generated CRC tail. The
60-byte object fits in one 1 KiB bank but not in a four-byte queue; streaming
it through such a queue takes at least 15 fills. This is capacity arithmetic,
not evidence that refill, simultaneous host access, pads or deadlines work.
It does not establish either design's area, whether the real macro or host can
support the trace, behavior with shared data, FIFO values/backpressure,
arbitration fairness, Ethernet frame capacity, or physical timing. Run a
matched RTL and physical comparison before selecting a memory organization.
