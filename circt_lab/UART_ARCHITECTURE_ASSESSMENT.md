# UART architecture assessment contract

This is an architecture experiment, not a UART compiler, executable bitstream,
or proof of UART support. The working fabric direction remains heterogeneous,
with configurable connections and distributed storage. This experiment helps
choose its capabilities; it does not select a final physical arrangement.

## Source requirements

`--uart-graph` currently recognizes a restricted, checked full-duplex UART
8N1 source component. It follows source bindings instead of fixed declaration
names and rejects unrecognized frames, control bindings, streams, and contexts.
The registered graph has the following operation contracts:

* `uart_tx_8n1`: consume an abstract host byte, generate a low start bit,
  payload bits least-significant first, then a high stop bit, and expose the
  source's busy/done signals. Idle is high. The byte transport is a boundary;
  this graph does not choose a FIFO, SRAM address, queue depth or handshake.
* `uart_rx_8n1`: start on the declared falling edge, use the declared RX level
  and timer tick to sample the framed input, and return an eight-bit payload
  through an abstract host boundary. Framing-error handling is not represented
  or claimed. The source example does not declare error handling.
* `falling_edge` represents the source's `rx.fall`; synchronization placement,
  edge-to-event latency, metastability and reset behavior are not yet mapped.
* `uart_timer` records the exact resolved source `period` and `first`, in
  source clock cycles, along with its start and stop dependencies. For the
  checked example, the 50 MHz source clock, 64-cycle period and TX/RX first
  values of 62 and 31 yield a nominal 781250 bit/s. These values describe the
  source timing; whether a proposed path meets the pin sampling instant is
  not checked by the resource assessment.

The two timer nodes and the two shifter nodes are marked exclusive for the
entire modeled configuration, expressing that this implementation needs TX
and RX to be able to run concurrently with independent state. This is a
conservative decomposition, not a proof that a shared or combined custom
block could not implement both. Abstract host endpoints are also distinct;
that is a modeling convenience, not a requirement for two physical host
interfaces.

The graph captures dependencies and widths, not a complete operational
semantics: feedback is explicit but isn't scheduled; reset, continuous byte
traffic, queueing, framing errors, pin timing, and full source functions are
not verified. It is marked `analysis_only`; `--map-graph` will not produce a
configuration from it. Source tests are not silently compiled into hardware.

## Two target descriptions

`candidates/memory-spi-graph.mlir` is a coarse resource/reachability model of
the *current experimental slice*, not the proposed completed fabric. Its one
physical timer, one shifter and two banks correspond to the RTL. Its adapter,
route and link abstractions do not preserve every port, selector or latency;
host stream operations are absent from this model, so missing host capability
cannot by itself be classified as missing hardware. `--assess-graph` reports
what this declared graph can support, not all implementations of UART that
might be devised.

`candidates/current-slice-rtl-contract.json` is the complementary RTL
inventory: actual instance counts, named timer/shifter/control/pin ports, all
eight route selector tables in select-index order, SRAM organization, and
known limitations. `test_rtl_inventory.py` checks the instance counts, port
bindings and selector expressions against the sister RTL checkout. It also
checks the coarse graph's timer and shifter counts. The test is a drift check,
not a formal semantic equivalence proof. The graph assessor does not yet
consume the selector inventory; it cannot prove port-level route legality.

`candidates/uart-capability-hypothesis.mlir` describes a **hypothetical**
capability and reachability graph with distinct UART timers and shifters,
falling-edge detection, abstract host endpoints and signal paths. These are
assumptions for an experiment, not RTL that has been built or a proposed final
block arrangement. Links are reachability, not an implementation of named
ports, selectors, shared wires, queues or physical locations. Its zero-cycle
link latencies are placeholders. Finding an abstract placement therefore
establishes only that this set of declared resources and paths satisfies the
current graph's structural constraints.

## Run and interpret

```sh
spatial frontend-mlir examples/uart.spt > /tmp/uart-source.mlir
spatial-circt-map --uart-graph /tmp/uart-source.mlir > /tmp/uart-work.mlir
spatial-circt-map --assess-graph circt_lab/candidates/memory-spi-graph.mlir /tmp/uart-work.mlir
spatial-circt-map --assess-graph circt_lab/candidates/uart-capability-hypothesis.mlir /tmp/uart-work.mlir
python3 circt_lab/test_rtl_inventory.py ../protocol-emulator
```

The current model reports `infeasible_in_model`, including the two-versus-one
timer and shifter counts, lack of compatible UART operations, and missing
abstract host/edge operations. These are **model deficits**; only the timer
and shifter instance counts are directly checked against RTL here, and a
missing abstract operation need not correspond to a single missing block.

The hypothetical model reports `abstract_placement_found`. The report
explicitly sets `rtl_fidelity_verified`, `timing_feasibility_verified`,
`physical_timing_verified`, `full_source_program_compiled`, and
`image_emitted` false. It deliberately omits the generic abstract one-shot
schedule: that schedule cannot establish periodic UART timing. Candidate
`uart_timer` tiles must declare allowed period and first-tick ranges, which
are checked against each source timer before placement.

`test_graphs.py` tests source mutations and renamed bindings, resource-count
and capability deficits, timer ranges, and missing directed paths. It compares
the candidate timer assignment with an independently enumerated two-timer
oracle; the general mapper is also compared against an exhaustive solver on
60 small graphs. These tests check the stated graph model, not pin timing or
RTL equivalence.

The next compiler/hardware contract work is port-level, static-selector and
shared-link routing, accurate pin/synchronizer timing, byte storage and host
arbitration, operation semantics including RX error behavior, and image
emission. A candidate design needs corresponding RTL and pin-level UART
simulation before any support or timing claim.
