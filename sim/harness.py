"""Run a compiled program's tests against the RTL, one fresh chip per test.

The plan comes from `spatial run --test`: the SRAM image, pin, stream and
function descriptions, and each test's wires, peers and steps. Results go
to a JSON file that `spatial` prints. A failing test never stops the others.
"""

import json
import os
from collections import Counter
from pathlib import Path

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import ClockCycles, Edge, FallingEdge, Lock, RisingEdge
from cocotb.utils import get_sim_time
from cocotb_tests.common import reset_top
from cocotb_tests.fabric import load_program, pop_rx_tagged, push_tx, select_channel, start_engine, tx_full

import peers

# A blocking step gives up after this many cycles.
STEP_TIMEOUT_CYCLES = 200_000
MAX_ASSERTED_PATTERNS = 4096


class Failure(Exception):
    """A failed expectation, reported against a source line."""


def fmt(data):
    if isinstance(data, dict):
        return f"packet({fmt(data['data'])}, error: {int(data['error'])})"
    return "[" + ", ".join(f"0x{b:02X}" for b in data) + "]"


class Bus:
    """The wires around the chip. Every pad is pulled up; the chip and the
    peers pull it low (a push-pull driver drives both ways, which a wired-AND
    models while only one side drives). `wire a -> b` makes pad b carry
    whatever pad a carries."""

    def __init__(self, dut, wires):
        self.dut = dut
        self.wires = {to: source for source, to in wires}
        self.drive = {}  # (pad, peer name) -> 0 or 1; absent means released

    def driven(self, pad):
        """Whether the chip or a peer drives the pad, rather than the pull-up."""
        pad = self.wires.get(pad, pad)
        return bool((int(self.dut.uio_oe.value) >> pad) & 1) or any(p == pad for p, _ in self.drive)

    def level(self, pad):
        pad = self.wires.get(pad, pad)
        chip = (int(self.dut.uio_out.value) >> pad) & 1 if (int(self.dut.uio_oe.value) >> pad) & 1 else 1
        return min([chip, *(level for (p, _), level in self.drive.items() if p == pad)])

    async def run(self):
        while True:
            await FallingEdge(self.dut.clk)
            self.dut.uio_in.value = sum(self.level(p) << p for p in range(8))


class Host:
    """The host's side of the host link, which carries one command at a time.
    Receive FIFOs hold only 4 bytes, so a background task keeps every
    receiving channel drained into a buffer of (byte, eop, err)."""

    def __init__(self, dut, channels):
        self.dut = dut
        self.link = Lock()
        self.received = {channel: [] for channel in sorted(channels)}

    async def write(self, channel, data, eop_last=False):
        for i, byte in enumerate(data):
            while True:
                async with self.link:
                    await select_channel(self.dut, channel)
                    if not await tx_full(self.dut):
                        await push_tx(self.dut, byte, eop=eop_last and i == len(data) - 1)
                        break
                await ClockCycles(self.dut.clk, 8)

    async def poll(self, channel):
        async with self.link:
            await select_channel(self.dut, channel)
            entry = await pop_rx_tagged(self.dut)
        if entry is not None:
            self.received[channel].append(entry)
        return entry

    async def drain(self):
        while True:
            for channel in self.received:
                await self.poll(channel)
            await ClockCycles(self.dut.clk, 4)

    async def flush(self, channel):
        """Collect everything the channel holds right now."""
        while await self.poll(channel) is not None:
            pass


async def wait_for(dut, ready, describe, check, timeout_cycles=STEP_TIMEOUT_CYCLES):
    """Wait until ready, checking the configured budget in 16-cycle increments."""
    for _ in range((timeout_cycles + 15) // 16):
        check()
        if ready():
            return
        await ClockCycles(dut.clk, 16)
    raise Failure(f"{describe()} after {timeout_cycles} cycles")


def host_value(expr, args, elements):
    """Evaluate a value the host computes from a call's arguments."""
    if isinstance(expr, int):
        return expr
    if "param" in expr:
        return args[expr["param"]]
    if "len" in expr:
        return len(args[expr["len"]])
    if "element" in expr:
        return elements[expr["element"]]
    a, b = host_value(expr["a"], args, elements), host_value(expr["b"], args, elements)
    return {"+": a + b, "-": a - b, "<<": a << b, ">>": a >> b, "|": a | b, "&": a & b, "^": a ^ b}[expr["op"]]


def call_bytes(sends, args, elements=None):
    """The bytes a call sends after its function id, mirroring the body's loops."""
    elements = elements or {}
    out = []
    for item in sends:
        if "value" in item:
            out.append(host_value(item["value"], args, elements))
        elif "for" in item:
            items = args[item["in"]]
            out.append(len(items))
            for element in items:
                out += call_bytes(item["body"], args, {**elements, item["for"]: element})
        else:
            count = host_value(item["repeat"], args, elements)
            out.append(count)
            for _ in range(max(count, 0)):
                out += call_bytes(item["body"], args, elements)
    return out


async def call(dut, host, calls, step, check, timeout_cycles=STEP_TIMEOUT_CYCLES):
    """Call an exported function; return (result bytes, fail message or None)."""
    name = step["function"]
    function = calls["functions"][name]
    args = step["args"]
    channel = calls["channel"]
    payload = call_bytes(function["sends"], args)
    if any(not 0 <= value <= 255 for value in payload):
        raise Failure(f"`{name}` would send {payload}; every value must fit in a byte")
    command = cocotb.start_soon(host.write(channel, [function["id"], *payload, 0], eop_last=True))
    writers = []
    for param in function["params"]:
        if param["type"] == "bytes" and param["via"] is not None:
            writers.append(cocotb.start_soon(host.write(param["via"], args[param["name"]])))
    replies = host.received[channel]
    await wait_for(
        dut,
        lambda: any(eop for _, eop, _ in replies),
        lambda: f"`{name}` got no answer (replies so far: {fmt(b for b, _, _ in replies)})",
        check, timeout_cycles,
    )
    end = next(i for i, (_, eop, _) in enumerate(replies) if eop)
    answer = [b for b, _, _ in replies[:end]]
    status, _, error = replies[end]
    del replies[: end + 1]
    # After a failure the chip discards the rest of the call, up to its
    # terminator, so the whole call must still be sent.
    await command
    for writer in writers:
        writer.cancel()
    if isinstance(function["result"], dict):
        buffer = host.received[function["result"]["channel"]]
        await host.flush(function["result"]["channel"])
        answer = [b for b, _, _ in buffer]
        buffer.clear()
    if not error:
        return answer, None
    if status == 0xFF:
        raise Failure(f"the chip did not recognize `{name}`")
    return answer, calls["fails"][status - 1]


class Invariants:
    """Check every `always` condition on every clock cycle."""

    def __init__(self, dut, bus, invariants):
        self.dut, self.bus, self.invariants = dut, bus, invariants
        self.problems = []

    def holds(self, node, now, before):
        if "const" in node:
            return bool(node["const"])
        if "edge" in node:
            # A line going from floating to driven is not an edge.
            (a, was_driven), (b, is_driven) = before[node["pin"]], now[node["pin"]]
            if not (was_driven and is_driven):
                return False
            return {"changed": a != b, "rise": not a and b, "fall": a and not b}[node["edge"]]
        if "pin" in node:
            return bool(now[node["pin"]][0])
        x = self.holds(node["a"], now, before)
        if node["op"] == "not":
            return not x
        y = self.holds(node["b"], now, before)
        return {"&&": x and y, "||": x or y, "==": x == y, "!=": x != y, "implies": not x or y}[node["op"]]

    async def run(self):
        before, cycle = None, 0
        while True:
            await RisingEdge(self.dut.clk)
            now = [(self.bus.level(p), self.bus.driven(p)) for p in range(8)]
            for invariant in self.invariants:
                if before is not None and not self.holds(invariant["check"], now, before):
                    self.problems.append((invariant, cycle))
            before, cycle = now, cycle + 1


class Session:
    """A chip, its wires, its peers and the host, from reset onward."""

    def __init__(self, dut, plan, period_ps, timeout_cycles=STEP_TIMEOUT_CYCLES):
        self.dut, self.plan, self.period_ps = dut, plan, period_ps
        self.timeout_cycles = timeout_cycles
        self.calls = plan["calls"]
        channels = {s["channel"] for s in plan["streams"].values() if s["direction"] == "from_chip"}
        if self.calls["channel"] is not None:
            channels.add(self.calls["channel"])
            channels |= {f["result"]["channel"] for f in self.calls["functions"].values() if isinstance(f["result"], dict)}
        self.bus = Bus(dut, [])
        self.host = Host(dut, channels)
        self.invariants = Invariants(dut, self.bus, plan["invariants"])
        self.packet_channels = {s["channel"] for s in plan["streams"].values()
                                if s.get("packets") and s["direction"] == "from_chip"}
        self.peers = {}
        self.uart_output_peer_names = set()
        self.tasks = []
        self.profile = Counter()
        self.profile_edges = plan.get("architecture", {}).get("signal_connections", [])
        self.edge_active_cycles = [0] * len(self.profile_edges)
        self.edge_activity_patterns = Counter()
        self.edge_activity_patterns_truncated_cycles = 0
        self.profile_byte_links = plan.get("architecture", {}).get("byte_path_links", [])
        self.profile_direct_links = plan.get("architecture", {}).get("direct_links", [])
        self.direct_link_absorb = [0] * len(self.profile_direct_links)
        self.direct_link_emit = [0] * len(self.profile_direct_links)
        self.memory_addresses = {"data": set(), "config": set(), "instruction": set()}
        self.memory_data_read_cycles = []
        self.memory_tx_push_events = []
        self.memory_stall_masks = []
        self.controller_trace = []
        self.event_timeline = []
        self.config_trace = []
        self.profile_gpio_trace = os.environ.get("SPATIAL_PROFILE_GPIO_TRACE") == "1"
        self.gpio_transition_intervals = [[] for _ in range(8)]
        self.gpio_transition_records = [[] for _ in range(8)]
        self.profile_byte_stages = [
            (stream["channel"], stage["tile"], stage["kind"])
            for stream in plan["streams"].values()
            for stage in stream.get("byte_stages", [])
        ]

    async def profile_rtl(self):
        # Explicitly RTL-only. This optional probe is never enabled by the
        # default compiler or the gate-level regression.
        core = self.dut.user_project.core
        controller = core.ctrl
        project = self.dut.user_project
        previous_gpio_out = int(self.dut.uio_out.value)
        previous_gpio_in = int(self.dut.uio_in.value)
        previous_controller_sample = None
        previous_gpio_event = int(self.dut.uio_out.value)
        last_config_values = {}
        tx_high_water = [0, 0]
        rx_high_water = [0, 0]
        last_gpio_transition_cycle = [None] * 8
        self.pending_data_reads = []
        gpio_transition_intervals = self.gpio_transition_intervals
        byte_stage_handles = {}
        for channel, tile_index, kind in self.profile_byte_stages:
            tile = core.stream[channel].tile[tile_index % 4].tile
            byte_stage_handles[tile_index] = (
                tile.in_valid, tile.in_ready, tile.out_valid, tile.out_ready, kind,
            )
        while True:
            await FallingEdge(self.dut.clk)
            self.profile["cycles"] += 1
            state = int(controller.state.value)
            self.profile[f"controller_state_{state}"] += 1
            op = int(controller.ir.value) >> 4
            active_shifter_mask = int(core.s_active.value)
            active_shifters = active_shifter_mask.bit_count()
            if int(controller.cfg_we.value):
                config_bank = int(core.write_bank.value)
                config_address = int(controller.cfg_addr.value)
                config_value = int(controller.cfg_data.value)
                config_key = (config_bank, config_address)
                self.profile["controller_config_writes"] += 1
                self.profile[f"controller_config_addr_{config_address:02x}_writes"] += 1
                if last_config_values.get(config_key) == config_value:
                    self.profile["controller_same_value_config_rewrites"] += 1
                    self.profile[f"controller_config_addr_{config_address:02x}_same_value_rewrites"] += 1
                last_config_values[config_key] = config_value
                if os.environ.get("SPATIAL_PROFILE_CONFIG_TRACE") == "1":
                    self.config_trace.append([
                        self.profile["cycles"], state, config_bank,
                        config_address, config_value,
                    ])
            if os.environ.get("SPATIAL_PROFILE_CONTROLLER_TRACE") == "1":
                sample = (
                    state, int(controller.pc.value), int(controller.ir.value),
                    int(controller.acc.value), int(controller.ptr.value),
                    int(controller.cnt.value), int(controller.channel.value),
                    int(controller.tx_space.value), int(controller.tx_push.value),
                    int(controller.tx_grant.value), int(core.s_active.value),
                    int(project.tx_empty.value), int(project.tx_full.value),
                    int(project.tx_level.value),
                )
                if sample != previous_controller_sample:
                    self.controller_trace.append([self.profile["cycles"], *sample])
                    previous_controller_sample = sample
            if os.environ.get("SPATIAL_PROFILE_EVENT_TRACE") == "1":
                gpio_out = int(self.dut.uio_out.value)
                gpio_oe = int(self.dut.uio_oe.value)
                timer_rise = int(core.t_rise.value)
                controller_fire = int(core.controller_fire.value)
                actor_actions = int(core.actor_actions.value)
                event_consume = int(controller.event_consume.value)
                event_ready = int(controller.event_ready.value)
                if (timer_rise or controller_fire or actor_actions or event_consume
                        or ((gpio_out ^ previous_gpio_event) & gpio_oe)):
                    self.event_timeline.append([
                        self.profile["cycles"], timer_rise,
                        int(controller.event_index.value), event_ready,
                        event_consume, controller_fire, actor_actions,
                        int(controller.pc.value), int(controller.ir.value),
                        gpio_out, gpio_oe, int(self.dut.uio_in.value),
                    ])
                previous_gpio_event = gpio_out
            if state == 1 and op == 10 and not int(controller.retire.value):
                self.profile["wait_event"] += 1
            if state == 1 and op == 14 and not int(controller.retire.value):
                self.profile["wait_fifo"] += 1
            sram_data_read = (
                int(project.memory_enable.value) and int(controller.data_read.value)
            )
            if int(project.memory_enable.value):
                address = int(project.memory_address.value)
                if sram_data_read:
                    self.profile["sram_data_read_cycles"] += 1
                    self.memory_addresses["data"].add(address)
                    memory_kind = "data"
                elif int(controller.block.value):
                    self.profile["sram_config_read_cycles"] += 1
                    self.memory_addresses["config"].add(address)
                    memory_kind = "config"
                else:
                    self.profile["sram_instruction_read_cycles"] += 1
                    self.memory_addresses["instruction"].add(address)
                    memory_kind = "instruction"
                self.profile[f"sram_{memory_kind}_read_shifters_{active_shifters}"] += 1
            if int(core.s_active.value):
                self.profile["shifter_active"] += 1
            self.profile[f"active_shifters_{active_shifters}"] += 1
            self.profile["max_concurrent_shifters"] = max(
                self.profile["max_concurrent_shifters"], active_shifters)
            controller_waiting = (
                state == 1
                and ((op == 10 or op == 14) and not int(controller.retire.value))
            )
            if state in (0, 1, 4) and not controller_waiting:
                self.profile[f"controller_service_shifters_{active_shifters}"] += 1
            if state == 1:
                self.profile["controller_execute_cycles"] += 1
                if int(controller.retire.value):
                    self.profile["controller_retire_cycles"] += 1
                    if active_shifters:
                        self.profile["controller_retire_shifter_active_cycles"] += 1
                    else:
                        self.profile["controller_retire_shifter_idle_cycles"] += 1
                if op == 10 and not int(controller.retire.value):
                    key = "controller_event_wait_shifter_active_cycles" if active_shifters else "controller_event_wait_shifter_idle_cycles"
                    self.profile[key] += 1
            if int(core.armed.value):
                self.profile["armed"] += 1
            tx_push = int(project.tx_push.value)
            tx_pop = int(project.tx_pop.value)
            rx_push = int(project.rx_push.value)
            rx_pop = int(project.rx_pop.value)
            core_tx_push = int(project.core_tx_push.value)
            host_tx_push = int(project.host_tx_push.value)
            if sram_data_read:
                self.pending_data_reads.append(self.profile["cycles"])
                self.memory_data_read_cycles.append(self.profile["cycles"])
            for channel in range(2):
                if (core_tx_push >> channel) & 1:
                    self.memory_tx_push_events.append(
                        (self.profile["cycles"], channel)
                    )
                    if self.pending_data_reads:
                        latency = self.profile["cycles"] - self.pending_data_reads.pop(0)
                        self.profile["sram_data_to_tx_push_pairs"] += 1
                        self.profile[f"sram_data_to_tx_push_channel_{channel}_pairs"] += 1
                        self.profile["sram_data_to_tx_push_latency_cycles"] += latency
                        pair_count = self.profile["sram_data_to_tx_push_pairs"]
                        if pair_count == 1:
                            self.profile["sram_data_to_tx_push_min_latency_cycles"] = latency
                        else:
                            self.profile["sram_data_to_tx_push_min_latency_cycles"] = min(
                                self.profile["sram_data_to_tx_push_min_latency_cycles"],
                                latency,
                            )
                        self.profile["sram_data_to_tx_push_max_latency_cycles"] = max(
                            self.profile["sram_data_to_tx_push_max_latency_cycles"], latency
                        )
                        bucket = min(latency // 32, 4)
                        bounds = ((0, 31), (32, 63), (64, 95), (96, 127), (128, None))
                        low, high = bounds[bucket]
                        label = f"sram_data_to_tx_push_latency_{low}_{high or 'plus'}_cycles"
                        self.profile[label] += 1
                    else:
                        self.profile["sram_data_to_tx_push_unmatched_pushes"] += 1
            self.profile["tx_fifo_pushes"] += tx_push.bit_count()
            self.profile["tx_fifo_pops"] += tx_pop.bit_count()
            self.profile["rx_fifo_pushes"] += rx_push.bit_count()
            self.profile["rx_fifo_pops"] += rx_pop.bit_count()
            self.profile["engine_tx_pushes"] += core_tx_push.bit_count()
            self.profile["host_tx_pushes"] += host_tx_push
            tx_levels = int(project.tx_level.value)
            rx_levels = int(project.rx_level.value)
            sh_tx_request = int(core.sh_tx_request.value)
            sh_tx_grant = int(core.sh_tx_grant.value)
            tx_empty = int(project.tx_empty.value)
            rx_stream_valid = int(core.rx_stream_valid.value)
            rx_full = int(project.rx_full.value)
            tx_empty_stalls = []
            tx_empty_stall_peer_active = []
            for channel in range(2):
                self.profile[f"tx_fifo_{channel}_pushes"] += (tx_push >> channel) & 1
                self.profile[f"tx_fifo_{channel}_pops"] += (tx_pop >> channel) & 1
                self.profile[f"engine_tx_fifo_{channel}_pushes"] += (core_tx_push >> channel) & 1
                tx_level = (tx_levels >> (4 * channel)) & 0xF
                rx_level = (rx_levels >> (4 * channel)) & 0xF
                tx_high_water[channel] = max(tx_high_water[channel], tx_level)
                rx_high_water[channel] = max(rx_high_water[channel], rx_level)
                self.profile[f"tx_fifo_{channel}_high_water"] = tx_high_water[channel]
                self.profile[f"rx_fifo_{channel}_high_water"] = rx_high_water[channel]
                shifter_waiting = (
                    ((sh_tx_request >> channel) & 1)
                    and not ((sh_tx_grant >> channel) & 1)
                )
                self.profile[f"shifter_{channel}_byte_wait_cycles"] += shifter_waiting
                empty_stall = shifter_waiting and ((tx_empty >> channel) & 1)
                peer_active = bool(active_shifter_mask & (1 << (1 - channel)))
                tx_empty_stalls.append(empty_stall)
                tx_empty_stall_peer_active.append(empty_stall and peer_active)
                self.profile[f"tx_fifo_{channel}_empty_stall_cycles"] += empty_stall
                self.profile[
                    f"tx_fifo_{channel}_empty_stall_peer_active_cycles"
                ] += empty_stall and peer_active
                self.profile[
                    f"tx_fifo_{channel}_empty_stall_data_read_cycles"
                ] += empty_stall and sram_data_read
                self.profile[f"rx_fifo_{channel}_full_stall_cycles"] += (
                    (rx_stream_valid >> channel) & 1
                ) and ((rx_full >> channel) & 1)
            any_tx_empty_stall = any(tx_empty_stalls)
            self.memory_stall_masks.append(
                sum((1 << channel) for channel, stalled in enumerate(tx_empty_stalls) if stalled)
            )
            self.profile["tx_fifo_any_empty_stall_cycles"] += any_tx_empty_stall
            self.profile["tx_fifo_both_empty_stall_cycles"] += all(tx_empty_stalls)
            self.profile["tx_fifo_any_empty_stall_peer_active_cycles"] += (
                any(tx_empty_stall_peer_active)
            )
            self.profile["tx_fifo_any_empty_stall_data_read_cycles"] += (
                any_tx_empty_stall and sram_data_read
            )
            gpio_out = int(self.dut.uio_out.value)
            gpio_oe = int(self.dut.uio_oe.value)
            gpio_in = int(self.dut.uio_in.value)
            self.profile["gpio_driven_pin_cycles"] += gpio_oe.bit_count()
            for pin, _peer_name in self.bus.drive:
                self.profile["external_driven_pin_cycles"] += 1
                self.profile[f"external_pin_{pin}_driven_cycles"] += 1
            changed_driven = (gpio_out ^ previous_gpio_out) & gpio_oe
            self.profile["gpio_output_pin_toggles"] += changed_driven.bit_count()
            # The harness mirrors resolved pad levels onto uio_in for every
            # pad, including outputs. Count transitions only on pads the chip
            # is not actively driving so output activity is not double-counted.
            changed_inputs = (gpio_in ^ previous_gpio_in) & ~gpio_oe & 0xFF
            self.profile["gpio_input_pin_changes"] += changed_inputs.bit_count()
            for pin in range(8):
                if (gpio_oe >> pin) & 1:
                    self.profile[f"gpio_pin_{pin}_driven_cycles"] += 1
                if (changed_driven >> pin) & 1:
                    self.profile[f"gpio_pin_{pin}_toggles"] += 1
                    if self.profile_gpio_trace:
                        previous_cycle = last_gpio_transition_cycle[pin]
                        if previous_cycle is not None:
                            gpio_transition_intervals[pin].append(
                                self.profile["cycles"] - previous_cycle
                            )
                        last_gpio_transition_cycle[pin] = self.profile["cycles"]
                        self.gpio_transition_records[pin].append(
                            [self.profile["cycles"], (gpio_out >> pin) & 1]
                        )
                if (changed_inputs >> pin) & 1:
                    self.profile[f"gpio_input_pin_{pin}_changes"] += 1
            for left in range(8):
                if not ((gpio_oe >> left) & 1):
                    continue
                for right in range(left + 1, 8):
                    if (gpio_oe >> right) & 1:
                        self.profile[f"gpio_pair_{left}_{right}_overlap_cycles"] += 1
                        if ((gpio_out >> left) & 1) == ((gpio_out >> right) & 1):
                            self.profile[f"gpio_pair_{left}_{right}_equal_cycles"] += 1
            previous_gpio_out = gpio_out
            previous_gpio_in = gpio_in
            for tile_index, (in_valid, in_ready, out_valid, out_ready, kind) in byte_stage_handles.items():
                incoming = int(in_valid.value)
                ready_in = int(in_ready.value)
                outgoing = int(out_valid.value)
                ready = int(out_ready.value)
                label = f"byte_stage_{tile_index}_{kind}"
                self.profile[f"{label}_input_valid_cycles"] += incoming
                self.profile[f"{label}_input_transfers"] += incoming and ready_in
                self.profile[f"{label}_output_valid_cycles"] += outgoing
                self.profile[f"{label}_output_transfers"] += outgoing and ready
                self.profile[f"{label}_backpressure_cycles"] += outgoing and not ready
            link_absorb = int(core.link_absorb.value)
            link_emit = int(core.link_emit.value)
            for index, edge in enumerate(self.profile_direct_links):
                shifter = int(edge["source_index"])
                self.direct_link_absorb[index] += (link_absorb >> shifter) & 1
                self.direct_link_emit[index] += (link_emit >> shifter) & 1
            line_values = int(core.lines.value)
            active_edge_mask = 0
            for index, edge in enumerate(self.profile_edges):
                if edge.get("destination_kind") == "shifter":
                    shifter = getattr(core, f"shifter_lines{edge['destination_index']}")
                    line_values = int(shifter.value)
                else:
                    line_values = int(core.lines.value)
                active = (line_values >> edge["line"]) & 1
                self.edge_active_cycles[index] += active
                if active:
                    active_edge_mask |= 1 << index
            if (
                active_edge_mask in self.edge_activity_patterns
                or len(self.edge_activity_patterns) < MAX_ASSERTED_PATTERNS
            ):
                self.edge_activity_patterns[active_edge_mask] += 1
            else:
                self.edge_activity_patterns_truncated_cycles += 1

    async def profile_direct_absorb(self):
        """Count CRC absorb pulses without sampling unrelated signals per cycle."""
        core = self.dut.user_project.core
        absorb = core.link_absorb
        while True:
            await Edge(absorb)
            value = int(absorb.value)
            for index, edge in enumerate(self.profile_direct_links):
                shifter = int(edge["source_index"])
                self.direct_link_absorb[index] += (value >> shifter) & 1

    async def profile_direct_emit(self):
        """Count CRC tail-bit requests without per-cycle sampling."""
        core = self.dut.user_project.core
        emit = core.link_emit
        while True:
            await Edge(emit)
            value = int(emit.value)
            for index, edge in enumerate(self.profile_direct_links):
                shifter = int(edge["source_index"])
                self.direct_link_emit[index] += (value >> shifter) & 1

    async def start(self):
        await reset_top(self.dut)
        await load_program(self.dut, self.plan["image"])
        self.tasks = [cocotb.start_soon(c) for c in (self.bus.run(), self.host.drain())]
        async with self.host.link:
            await start_engine(self.dut)
        if (os.environ.get("SPATIAL_PROFILE_RTL") == "1"
                and os.environ.get("GATES") != "yes"
                and os.environ.get("GL_TEST") not in ("1", "yes")
                and hasattr(self.dut.user_project, "core")):
            if os.environ.get("SPATIAL_PROFILE_DIRECT_ONLY") == "1":
                self.profile["direct_link_profile"] = 1
                self.tasks.append(cocotb.start_soon(self.profile_direct_absorb()))
                self.tasks.append(cocotb.start_soon(self.profile_direct_emit()))
            else:
                self.tasks.append(cocotb.start_soon(self.profile_rtl()))
        self.tasks.append(cocotb.start_soon(self.invariants.run()))

    def connect(self, wires, specs):
        """Add wires and attach peers not attached yet; return an error or None."""
        self.bus.wires.update({to: source for source, to in wires})
        for spec in specs:
            if spec["name"] in self.peers:
                continue
            peer = peers.attach(spec, self.bus, self.dut)
            if isinstance(peer, str):
                return {"line": spec["line"], "message": peer}
            self.peers[spec["name"]] = peer
            self.tasks += [cocotb.start_soon(c) for c in peer.tasks()]
        return None

    def check(self):
        for name, peer in self.peers.items():
            if peer.problems:
                detail = f"`{name}`: {peer.problems[0]}"
                if os.environ.get("SPATIAL_PROFILE_RTL") == "1" and hasattr(peer, "received"):
                    detail += f" after receiving {len(peer.received)} bytes: {peer.received}"
                raise Failure(detail)
        for channel, buffer in self.host.received.items():
            if channel != self.calls["channel"] and channel not in self.packet_channels and any(err for _, _, err in buffer):
                raise Failure(f"channel {channel} received a byte marked as an error (wrong stop or parity bit)")
        if self.invariants.problems:
            invariant, cycle = self.invariants.problems[0]
            raise Failure(f"always \"{invariant['name']}\" (line {invariant['line']}) broke at cycle {cycle}")

    async def step(self, step):
        """Run one step; return the bytes it produced when nothing was expected of them."""
        dut, plan, host = self.dut, self.plan, self.host
        target = step.get("target", {})
        peer = self.peers.get(target.get("peer"))
        uart_bytes_read = 0
        output = None
        if step["op"] == "write_packet":
            await host.write(plan["streams"][target["stream"]]["channel"], step["bytes"], eop_last=True)
        elif step["op"] == "read_packet":
            name = target["stream"]
            buffer = host.received[plan["streams"][name]["channel"]]
            await wait_for(dut, lambda: any(eop for _, eop, _ in buffer),
                           lambda: f"`{name}` has {len(buffer)} bytes but no packet end", self.check, self.timeout_cycles)
            end = next(i + 1 for i, (_, eop, _) in enumerate(buffer) if eop)
            entries = buffer[:end]
            del buffer[:end]
            output = {"data": [b for b, _, _ in entries], "error": any(err for _, _, err in entries)}
        elif step["op"] == "write" and peer:
            peer.write(step["bytes"])
        elif step["op"] == "write":
            await host.write(plan["streams"][target["stream"]]["channel"], step["bytes"])
        elif step["op"] == "read":
            name = target.get("peer") or target["stream"]
            count = step["count"]
            if peer:
                buffer = peer.received
                values = lambda: list(buffer)
            else:
                buffer = host.received[plan["streams"][name]["channel"]]
                values = lambda: [b for b, _, _ in buffer]
            await wait_for(dut, lambda: len(buffer) >= count, lambda: f"`{name}` received {fmt(values())}, then nothing", self.check, self.timeout_cycles)
            output = values()[:count]
            del buffer[:count]
            if isinstance(peer, peers.UartDevice):
                uart_bytes_read = len(output)
                if uart_bytes_read:
                    self.uart_output_peer_names.add(peer.name)
                    if self.profile:
                        self.profile["uart_line_busy_cycles_read"] += (
                            uart_bytes_read * (10 + (peer.parity != "none")) * peer.bit
                        )
        elif step["op"] == "call":
            output, failed = await call(dut, host, self.calls, step, self.check, self.timeout_cycles)
            if "fails" in step and failed != step["fails"]:
                outcome = f'failed with "{failed}"' if failed else "succeeded"
                raise Failure(f"expected `{step['function']}` to fail with \"{step['fails']}\", but it {outcome}")
            if "fails" not in step and failed:
                raise Failure(f"`{step['function']}` failed: {failed}")
        elif step["op"] == "wait":
            await ClockCycles(dut.clk, step["cycles"])
        self.check()
        if "expect" in step:
            if output != step["expect"]:
                raise Failure(f"expected {fmt(step['expect'])}, got {fmt(output)}")
            if uart_bytes_read and self.profile:
                self.profile["uart_payload_bytes_read"] += uart_bytes_read
            return None
        if uart_bytes_read and self.profile:
            self.profile["uart_payload_bytes_read"] += uart_bytes_read
        return output

    def finish(self):
        for peer in self.peers.values():
            finish = getattr(peer, "finish", None)
            if finish is not None:
                finish()
        self.check()

    def stop(self):
        for task in self.tasks:
            task.cancel()


async def run_test(dut, plan, test, period_ps):
    session = Session(dut, plan, period_ps, test.get("step_timeout_cycles") or STEP_TIMEOUT_CYCLES)
    await session.start()
    error = session.connect(test["wires"], test["peers"])
    if error:
        session.stop()
        return error, 0, None
    started = get_sim_time("ps")
    line = test["line"]
    try:
        for step in test["steps"]:
            line = step["line"]
            await session.step(step)
        session.finish()
        error = None
    except Failure as failure:
        error = {"line": line, "message": str(failure)}
    finally:
        if session.profile:
            if os.environ.get("SPATIAL_PROFILE_DIRECT_ONLY") == "1":
                session.profile["cycles"] = int(get_sim_time("ps") - started) // period_ps
            if os.environ.get("SPATIAL_PROFILE_GPIO_TRACE") == "1":
                session.profile["gpio_transition_intervals"] = session.gpio_transition_intervals
                session.profile["gpio_transition_records"] = session.gpio_transition_records
            if session.profile_edges:
                session.profile["connection_active_cycles"] = session.edge_active_cycles
                session.profile["connection_edges"] = session.profile_edges
                session.profile["connection_asserted_patterns"] = [
                    {
                        "active_edges": [
                            index
                            for index in range(len(session.profile_edges))
                            if (mask >> index) & 1
                        ],
                        "cycles": cycles,
                    }
                    for mask, cycles in sorted(session.edge_activity_patterns.items())
                ]
                session.profile["connection_asserted_patterns_truncated_cycles"] = (
                    session.edge_activity_patterns_truncated_cycles
                )
            session.profile["sram_unique_addresses"] = {
                kind: len(addresses) for kind, addresses in session.memory_addresses.items()
            }
            pending_reads = list(session.memory_data_read_cycles)
            refill_windows = [set(), set()]
            for push_cycle, channel in session.memory_tx_push_events:
                if not pending_reads:
                    continue
                read_cycle = pending_reads.pop(0)
                refill_windows[channel].update(range(read_cycle, push_cycle))
            for channel in range(2):
                empty_stalls_in_window = sum(
                    bool(session.memory_stall_masks[cycle - 1] & (1 << channel))
                    for cycle in refill_windows[channel]
                    if 0 < cycle <= len(session.memory_stall_masks)
                )
                session.profile[
                    f"tx_fifo_{channel}_empty_stalls_between_data_read_and_push"
                ] = empty_stalls_in_window
                session.profile[
                    f"sram_data_to_tx_push_window_cycles_channel_{channel}"
                ] = len(refill_windows[channel])
            if os.environ.get("SPATIAL_PROFILE_CONTROLLER_TRACE") == "1":
                session.profile["controller_trace"] = session.controller_trace
            if os.environ.get("SPATIAL_PROFILE_EVENT_TRACE") == "1":
                session.profile["event_timeline"] = session.event_timeline
            if os.environ.get("SPATIAL_PROFILE_CONFIG_TRACE") == "1":
                session.profile["controller_config_trace"] = session.config_trace
            byte_path_transfers = []
            for edge in session.profile_byte_links:
                destination_tile = edge.get("destination_tile")
                if destination_tile is not None:
                    tile = destination_tile
                    direction = "input"
                else:
                    tile = edge.get("source_tile")
                    direction = "output"
                stage = next(
                    (entry for entry in session.profile_byte_stages if entry[1] == tile), None
                )
                if stage is None:
                    byte_path_transfers.append(0)
                    continue
                label = f"byte_stage_{tile}_{stage[2]}_{direction}_transfers"
                byte_path_transfers.append(session.profile[label])
            session.profile["byte_path_transfers"] = byte_path_transfers
            session.profile["direct_link_activity"] = [
                {"absorb": absorb, "emit": emit}
                for absorb, emit in zip(
                    session.direct_link_absorb, session.direct_link_emit
                )
            ]
            session.profile["uart_payload_bytes"] = session.profile.get(
                "uart_payload_bytes_read", 0
            )
            session.profile["uart_output_endpoints"] = len(
                session.uart_output_peer_names
            )
            session.profile["uart_line_busy_cycles"] = session.profile.get(
                "uart_line_busy_cycles_read", 0
            )
            session.profile["sram_data_read_unpaired_at_end"] = len(
                session.pending_data_reads
            )
            print(f"RTL_PROFILE {test['name']} {json.dumps(session.profile)}", flush=True)
        session.stop()
    return error, int(get_sim_time("ps") - started) // period_ps, (dict(session.profile) or None)


async def console(dut, plan, period_ps, requests_path, replies_path):
    """Serve one request per line: {"wires", "peers", "steps"} -> {"outputs"} or {"error"}.
    The simulation waits while no request is pending, so chip time stands still
    between commands."""
    session = Session(dut, plan, period_ps)
    await session.start()
    with open(requests_path) as requests, open(replies_path, "w") as replies:
        replies.write(json.dumps({"ready": True}) + "\n")
        replies.flush()
        for request in requests:
            request = json.loads(request)
            started = get_sim_time("ps")
            reply = {"error": session.connect(request["wires"], request["peers"])}
            if not reply["error"]:
                try:
                    reply["outputs"] = [await session.step(step) for step in request["steps"]]
                except Failure as failure:
                    reply["error"] = {"message": str(failure)}
            reply["cycles"] = int(get_sim_time("ps") - started) // period_ps
            replies.write(json.dumps(reply) + "\n")
            replies.flush()
    session.stop()


@cocotb.test()
async def spatial(dut):
    plan = json.loads(Path(os.environ["SPATIAL_PLAN"]).read_text())
    period_ps = 10**12 // plan["clock_hz"]
    cocotb.start_soon(Clock(dut.clk, period_ps, unit="ps").start())
    if os.environ.get("SPATIAL_CONSOLE"):
        requests_path, replies_path = os.environ["SPATIAL_CONSOLE"].split(os.pathsep)
        await console(dut, plan, period_ps, requests_path, replies_path)
        return
    only = os.environ.get("SPATIAL_ONLY")
    results = []
    for test in plan["tests"]:
        if not only or test["name"] == only:
            error, cycles, profile = await run_test(dut, plan, test, period_ps)
            result = {"name": test["name"], "error": error, "cycles": cycles}
            # The optional RTL-only probe measures controller occupancy and
            # concurrent shifter use for this test. Keep it in the machine
            # readable result as well as the simulator log.
            if profile is not None:
                result["profile"] = profile
            results.append(result)
    Path(os.environ["SPATIAL_RESULTS"]).write_text(json.dumps(results))
