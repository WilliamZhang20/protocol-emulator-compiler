//! The `spatial` command: create, check, build and test programs.
mod console;
mod preview;
mod project;
mod simulator;

use clap::{Parser, Subcommand};
use project::Project;
use std::{
    fs,
    io::{self, IsTerminal},
    path::{Path, PathBuf},
};

type AppResult<T> = Result<T, Box<dyn std::error::Error>>;

#[derive(Parser)]
#[command(
    version,
    about = "Compile and test programs for the spatial protocol chip"
)]
struct Cli {
    #[command(subcommand)]
    command: Command,
}

#[derive(Subcommand)]
enum Command {
    /// Export the existing Rust frontend's unallocated program to registered MLIR operations.
    FrontendMlir { file: PathBuf },
    /// Extract logical program IR without allocating target resources.
    ModelIr { file: PathBuf },
    /// Assess logical state machines against a hypothetical control tile.
    ModelProtocol {
        /// Spatial source program.
        file: PathBuf,
        /// Maximum minimized state count per tile.
        #[arg(long)]
        max_states: usize,
        /// Maximum one-bit inputs per tile.
        #[arg(long)]
        max_inputs: usize,
        /// Maximum one-based action identifier per tile.
        #[arg(long)]
        max_action: usize,
        /// Hypothetical input-sample-to-action latency in cycles.
        #[arg(long)]
        response_cycles: Option<u64>,
        /// Maximum permitted input-sample-to-action latency in cycles.
        #[arg(long)]
        response_deadline: Option<u64>,
    },
    /// Export the compiled, partial resource and dependency graph as JSON.
    ModelGraph { file: PathBuf },
    /// Compare abstract memory organizations using an explicit request graph.
    ModelMemory {
        /// JSON file describing candidate banks and causal memory requests.
        file: PathBuf,
    },
    /// Compare host-fed vector output with single-row and ping-pong storage.
    ModelStream {
        /// Output cells in each visible line.
        #[arg(long, default_value_t = 80)]
        cells: u64,
        /// Number of visible scanlines.
        #[arg(long, default_value_t = 480)]
        visible_lines: u64,
        /// Scanlines that repeat each logical input row.
        #[arg(long, default_value_t = 8)]
        row_repeat: u64,
        /// Independent payload bits emitted simultaneously at each cell.
        #[arg(long, default_value_t = 6)]
        independent_data_lanes: u64,
        /// Pads assigned to payload, including duplicated values.
        #[arg(long, default_value_t = 6)]
        data_pads: u64,
        /// Pads assigned to sync or other sideband signals.
        #[arg(long, default_value_t = 2)]
        sideband_pads: u64,
        /// Total available GPIO pads.
        #[arg(long, default_value_t = 8)]
        total_pads: u64,
        /// Cycles between output cells.
        #[arg(long, default_value_t = 16)]
        cell_cycles: u64,
        /// Non-active cycles per line.
        #[arg(long, default_value_t = 320)]
        horizontal_blank: u64,
        /// Total lines in a frame, including vertical blanking.
        #[arg(long, default_value_t = 525)]
        frame_lines: u64,
        /// Host cycles per transferred byte.
        #[arg(long, default_value_t = 10)]
        host_cycles_per_byte: u64,
        /// Existing output FIFO capacity in bytes.
        #[arg(long, default_value_t = 8)]
        fifo_depth: u64,
        /// Bytes per frame required by a competing host-fed stream.
        #[arg(long, default_value_t = 0)]
        concurrent_host_bytes: u64,
    },
    /// Print the program's tests and invariants as a hardware-independent plan.
    /// A backend's chip runner replays them at its own pins.
    TestPlan { file: PathBuf },
    /// Create a new project directory with a starter `main.spt`.
    New { name: PathBuf },
    /// Check that the program fits the chip, without writing anything.
    Check {
        file: Option<PathBuf>,
        /// Score configured edges against an OpenDB coordinate JSON map.
        #[arg(long, value_name = "JSON")]
        physical_map: Option<PathBuf>,
    },
    /// Compile into `target/image.bin`.
    Build { file: Option<PathBuf> },
    /// Build, then talk to the program on a simulated chip; `--test` runs its tests instead.
    Run {
        /// Run the tests; `--test=NAME` runs only that one.
        #[arg(long, num_args = 0..=1, require_equals = true, default_missing_value = "")]
        test: Option<String>,
        /// Compile the best timer-index candidate from an OpenDB coordinate map.
        #[arg(long, value_name = "JSON")]
        physical_map: Option<PathBuf>,
        file: Option<PathBuf>,
    },
    /// Show the Klein bottle animation.
    Preview {
        /// Print one frame instead of animating.
        #[arg(long)]
        snapshot: bool,
    },
}

/// A compile error already rendered with its source lines; printed as is.
#[derive(Debug)]
struct Reported(String);

impl std::fmt::Display for Reported {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(&self.0)
    }
}

impl std::error::Error for Reported {}

/// Whether to color what goes to a stream: only a terminal, and never under
/// `NO_COLOR` (<https://no-color.org>).
fn color(terminal: bool) -> bool {
    terminal && std::env::var_os("NO_COLOR").is_none_or(|v| v.is_empty())
}

fn paint(on: bool, code: &str, text: &str) -> String {
    if on && !text.is_empty() {
        format!("\x1b[{code}m{text}\x1b[0m")
    } else {
        text.to_owned()
    }
}

/// How full each kind of hardware is, as a bar, then which pad each pin got:
///
/// ```text
///   pins       ██░░░░░░   2 / 8
///   timers     ██░░       2 / 4
///   shifters   ██         2 / 2   full
///   memory     ▍░░░░░░░   57 / 1024 bytes
///
///   Pins
///     tx    uio0
///     rx    uio1
/// ```
fn summary(build: &spatial::Build, on: bool) -> String {
    const WIDTH: usize = 8;
    const EIGHTHS: [char; 8] = [' ', '▏', '▎', '▍', '▌', '▋', '▊', '▉'];
    let rows: Vec<_> = build
        .usage
        .iter()
        .filter(|(_, used, _)| *used > 0)
        .collect();
    let label = rows.iter().map(|(name, ..)| name.len()).max().unwrap_or(0);
    let mut out = Vec::new();
    for &&(name, used, total) in &rows {
        // One cell per unit, or a scaled bar when there are too many units.
        let (filled, cells) = if total <= WIDTH {
            ("█".repeat(used), total)
        } else {
            let eighths = (used * WIDTH * 8).div_ceil(total).max(1);
            let partial = EIGHTHS[eighths % 8];
            let filled = "█".repeat(eighths / 8) + &partial.to_string();
            (filled.trim_end().to_owned(), WIDTH)
        };
        let empty = "░".repeat(cells.saturating_sub(filled.chars().count()));
        let pad = " ".repeat(WIDTH - cells);
        let unit = if name == "memory" { " bytes" } else { "" };
        let full = if used == total {
            paint(on, "33", "   full")
        } else {
            String::new()
        };
        out.push(format!(
            "  {name:<label$}   {}{}{pad}   {used} / {total}{unit}{full}",
            paint(on, "32", &filled),
            paint(on, "2", &empty),
        ));
    }
    if !build.pins.is_empty() {
        let mut pins: Vec<_> = build.pins.iter().collect();
        pins.sort_by_key(|(_, pad)| **pad);
        let width = pins.iter().map(|(name, _)| name.len()).max().unwrap_or(0);
        out.push(String::new());
        out.push(format!("  {}", paint(on, "1", "Pins")));
        for (name, pad) in pins {
            out.push(format!(
                "    {name:<width$}   {}",
                paint(on, "36", &format!("uio{pad}"))
            ));
        }
    }
    if !build.resources.is_empty() {
        out.push(String::new());
        out.push(format!(
            "  {}",
            paint(on, "1", "Hardware map (P = pulse line, L = level line)")
        ));
        for resource in &build.resources {
            let location = resource.config.map_or_else(
                || "controller".to_owned(),
                |address| format!("cfg 0x{address:02X}"),
            );
            let outputs = resource
                .outputs
                .iter()
                .map(|(name, line)| {
                    let line = if *line < 32 {
                        format!("P{line}")
                    } else {
                        format!("L{line}")
                    };
                    format!("{name}={line}")
                })
                .collect::<Vec<_>>()
                .join(" ");
            out.push(format!(
                "    {:<11} {:<18} {:<13} {outputs}",
                format!("{}[{}]", resource.kind, resource.index),
                resource.name,
                location,
            ));
        }
    }
    if !build.signal_connections.is_empty() {
        out.push(String::new());
        out.push(format!(
            "  {}",
            paint(
                on,
                "1",
                "Static topology model (configured dependencies; incomplete and not PPA)"
            )
        ));
        let base_edges = build
            .signal_connections
            .iter()
            .filter(|edge| edge.context.is_none() && edge.line.is_some())
            .collect::<Vec<_>>();
        let named_edges = base_edges
            .iter()
            .filter(|edge| edge.source.is_some())
            .count();
        let contextual_edges = build
            .signal_connections
            .iter()
            .filter(|edge| edge.context.is_some())
            .count();
        out.push(format!(
            "    base signal-line selector edges: {} ({} with named producers); context selectors: {}",
            base_edges.len(),
            named_edges,
            contextual_edges,
        ));
        let direct_links = build
            .signal_connections
            .iter()
            .filter(|edge| edge.context.is_none() && edge.transport == "direct_link")
            .count();
        out.push(format!("    base direct non-line links: {direct_links}"));
        let byte_links = build
            .signal_connections
            .iter()
            .filter(|edge| edge.context.is_none() && edge.transport == "byte_link")
            .count();
        out.push(format!("    base byte-path links: {byte_links}"));
        for edge in &build.signal_connections {
            let source = edge
                .source
                .as_deref()
                .map(str::to_owned)
                .unwrap_or_else(|| format!("unresolved_{}", edge.transport));
            let context = edge
                .context
                .map_or(String::new(), |bank| format!(" context{bank}"));
            let transport = edge.line.map_or_else(
                || edge.transport.to_owned(),
                |line| {
                    if line < 32 {
                        format!("P{line}")
                    } else {
                        format!("L{line}")
                    }
                },
            );
            out.push(format!(
                "      {source} -> {}.{} [{transport}]{}",
                edge.destination, edge.input, context,
            ));
        }
        for estimate in &build.topology_estimates {
            out.push(format!(
                "    {:<20} local={} cross-cluster={} estimated-hops={}",
                estimate.topology,
                estimate
                    .local_connections
                    .map_or_else(|| "unknown".to_owned(), |n| n.to_string()),
                estimate
                    .cross_cluster_connections
                    .map_or_else(|| "unknown".to_owned(), |n| n.to_string()),
                estimate
                    .estimated_hops
                    .map_or_else(|| "unknown".to_owned(), |n| n.to_string()),
            ));
            out.push(format!("      assumption: {}", estimate.assumptions));
            if estimate.topology.starts_with("graph_search_") {
                let assignment = estimate
                    .assignments
                    .iter()
                    .map(|(name, cluster)| format!("{name}=cluster{cluster}"))
                    .collect::<Vec<_>>()
                    .join(" ");
                out.push(format!("      hypothetical assignment: {assignment}"));
            }
        }
        let link_width_sweep = spatial::estimate_interconnect_widths(
            &build.resources,
            &build.signal_connections,
            &[1, 2, 4, 8, 16, 32],
        );
        for topology in [
            "channel_split",
            "stage_hierarchy",
            "small_mesh_2x2",
            "graph_search_2",
            "graph_search_4",
            "graph_search_4_width",
            "graph_search_4_width_activity",
        ] {
            let rows: Vec<_> = link_width_sweep
                .iter()
                .filter(|estimate| estimate.topology == topology)
                .collect();
            if let Some(first) = rows.first() {
                let widths = rows
                    .iter()
                    .map(|estimate| {
                        if estimate.over_capacity_bundles.is_empty() {
                            format!("{}:fits", estimate.capacity_per_direction)
                        } else {
                            format!(
                                "{}:{}",
                                estimate.capacity_per_direction,
                                estimate.over_capacity_bundles.len()
                            )
                        }
                    })
                    .collect::<Vec<_>>()
                    .join(" ");
                out.push(format!(
                    "    {topology} escape bundles: max width={} scalar lanes, routed lane-segments={}; width sweep [lanes:over-capacity bundles, fits otherwise]: {widths}",
                    first.maximum_bundle_width,
                    first.total_intercluster_net_segments,
                ));
                if !first.over_capacity_bundles.is_empty() {
                    out.push(format!(
                        "      one-lane bottlenecks: {}",
                        first
                            .over_capacity_bundles
                            .iter()
                            .map(|(link, nets)| format!("{link}={nets}"))
                            .collect::<Vec<_>>()
                            .join(" "),
                    ));
                }
            }
        }
    }
    if !build.protocol_mappings.is_empty() {
        out.push(String::new());
        out.push(format!(
            "  {}",
            paint(
                on,
                "1",
                "Protocol mapping candidates (parallelism, locality; controller latency is variable)"
            )
        ));
        for (index, candidate) in build
            .protocol_placement_search
            .candidates
            .iter()
            .enumerate()
        {
            let assignments = candidate
                .assignments
                .iter()
                .map(|(name, actor)| format!("{name}->actor{actor}"))
                .collect::<Vec<_>>()
                .join(" ");
            let assignments = if assignments.is_empty() {
                "controller-loop".to_owned()
            } else {
                assignments
            };
            let selected = if build.protocol_placement_search.selected == Some(index) {
                " selected"
            } else {
                ""
            };
            let feasible = if candidate.feasible {
                "feasible"
            } else {
                "infeasible"
            };
            out.push(format!(
                "    candidate {index}: {} {feasible} {assignments} parallel={} local-pairs={} actor-cycles={:?} controller-service-cycles={:?} input-interval-cycles={:?}{}",
                candidate.implementation,
                candidate.parallel_actors,
                candidate.local_channel_connections,
                candidate.actor_pipeline_cycles,
                candidate.controller_service_cycles,
                candidate.minimum_input_interval_cycles,
                paint(on, "32", selected),
            ));
        }
        for mapping in &build.protocol_mappings {
            let input = if mapping.input_line < 32 {
                format!("P{}", mapping.input_line)
            } else {
                format!("L{}", mapping.input_line)
            };
            let input2 = mapping.input_line2.map_or_else(String::new, |line| {
                if line < 32 {
                    format!(",P{line}")
                } else {
                    format!(",L{line}")
                }
            });
            let parallel_actors = build
                .protocol_placement_search
                .selected
                .and_then(|selected| build.protocol_placement_search.candidates.get(selected))
                .map_or(0, |candidate| candidate.parallel_actors);
            let mode = if mapping.actor.is_none() {
                "PX8"
            } else if mapping.wide_state {
                "8-state/1-input"
            } else {
                "4-state (1-2 inputs)"
            };
            out.push(format!(
                "    {:<18} {} mode={mode} input={input}{input2:<4} states={}->{} (reduced {}) rows={}->{} candidates={} parallel={} local-pairs={} actor-cycles={:?} controller-service-cycles={:?} input-interval-cycles={:?}",
                mapping.name,
                mapping.actor.map_or(String::from("PX8"), |actor| format!("actor{actor}")),
                mapping.source_states,
                mapping.mapped_states,
                mapping.states_reduced,
                mapping.source_transitions,
                mapping.mapped_transitions,
                mapping.placement_candidates_evaluated,
                parallel_actors,
                mapping.local_channel_connections,
                mapping.actor_pipeline_cycles,
                mapping.controller_service_cycles,
                mapping.minimum_input_interval_cycles,
            ));
        }
    }
    out.join("\n")
}

/// `text` as one shell word: as is when that is safe, otherwise quoted.
fn shell_quote(text: &str) -> String {
    let safe = |c: char| c.is_ascii_alphanumeric() || "-_./=:,+@%".contains(c);
    if !text.is_empty() && text.chars().all(safe) {
        text.to_owned()
    } else if !text.contains(['"', '\\', '$', '`', '!']) {
        format!("\"{text}\"")
    } else {
        format!("'{}'", text.replace('\'', r"'\''"))
    }
}

/// Compile and print the hardware summary.
fn build(project: &Project) -> AppResult<spatial::Build> {
    let build = spatial::compile(&project.text).map_err(|e| {
        let on = color(io::stderr().is_terminal());
        Reported(e.render(&project.display_path(), &project.text, on))
    })?;
    println!("{}", summary(&build, color(io::stdout().is_terminal())));
    Ok(build)
}

/// Compile the lowest-distance timer assignment reported by a physical map.
fn build_best_physical_candidate(project: &Project, path: &Path) -> AppResult<spatial::Build> {
    let bytes = fs::read(path)?;
    let placement: spatial::PhysicalPlacementMap = serde_json::from_slice(&bytes)?;
    let baseline = spatial::compile(&project.text).map_err(|error| {
        Reported(error.render(
            &project.display_path(),
            &project.text,
            color(io::stderr().is_terminal()),
        ))
    })?;
    let candidates = spatial::search_physical_index_assignments(
        &baseline.resources,
        &baseline.pins,
        &baseline.signal_connections,
        &placement,
        "timer",
    );
    let best = candidates
        .iter()
        .find(|candidate| candidate.estimate.mapped_edges > 0)
        .ok_or("the placement map has no located edges for a timer assignment")?;
    let overrides: std::collections::BTreeMap<_, _> = best.assignments.iter().cloned().collect();
    let candidate =
        spatial::compile_with_resource_indices(&project.text, &overrides).map_err(|error| {
            Reported(error.render(
                &project.display_path(),
                &project.text,
                color(io::stderr().is_terminal()),
            ))
        })?;
    let timer_map = candidate
        .resources
        .iter()
        .filter(|resource| resource.kind == "timer")
        .map(|resource| format!("{}=timer{}", resource.name, resource.index))
        .collect::<Vec<_>>()
        .join(" ");
    println!(
        "Compiled physical-index candidate: {timer_map}; locality proxy {:.3} µm (baseline {:.3} µm).",
        best.estimate.total_manhattan_um,
        spatial::estimate_physical_locality(
            &baseline.resources,
            &baseline.pins,
            &baseline.signal_connections,
            &placement,
        )
        .total_manhattan_um,
    );
    Ok(candidate)
}

/// One line per test, then a tally. Why a test failed is printed when only
/// that test ran or only that test failed, or when the output is a log rather
/// than a terminal; otherwise the last lines say how to run each failed test
/// on its own.
///
/// ```text
///   running 2 tests
///   test loopback ............. pass  (39.6 us)
///   test talks to a terminal .. FAIL  (52.7 us)
///
///   1 passed, 1 failed
///
///   To see why a test failed, run it on its own:
///     spatial run --test="talks to a terminal" examples/uart.spt
/// ```
fn report(
    project: &Project,
    file: Option<&Path>,
    build: &spatial::Build,
    outcomes: &[simulator::Outcome],
    only: Option<&str>,
) -> bool {
    let terminal = io::stdout().is_terminal();
    let on = color(terminal);
    let clock_hz = build.plan["clock_hz"].as_f64().unwrap_or(1.0);
    let tests = |n: usize| if n == 1 { "test" } else { "tests" };
    let total = build.tests.len();
    match only {
        Some(_) if total > 1 => println!("  running 1 of {total} tests"),
        _ => println!("  running {} {}", outcomes.len(), tests(outcomes.len())),
    }
    let width = outcomes
        .iter()
        .map(|o| o.name.chars().count())
        .max()
        .unwrap_or(0);
    for outcome in outcomes {
        let dots = ".".repeat(width - outcome.name.chars().count() + 2);
        let status = match outcome.error {
            None => paint(on, "32", "pass"),
            Some(_) => paint(on, "1;31", "FAIL"),
        };
        let micros = outcome.cycles as f64 * 1e6 / clock_hz;
        println!("  test {} {dots} {status}  ({micros:.1} us)", outcome.name);
        if let Some(profile) = &outcome.profile {
            let count = |key: &str| profile[key].as_u64().unwrap_or(0);
            if count("direct_link_profile") != 0 {
                println!(
                    "    RTL direct-link event profile: {} cycles",
                    count("cycles")
                );
            } else {
                println!(
                    "    RTL profile: {} cycles; sequencer event-wait {}%, FIFO-wait {}%; shifter active {}%; max concurrent shifters {}",
                    count("cycles"),
                    percent(count("wait_event"), count("cycles")),
                    percent(count("wait_fifo"), count("cycles")),
                    percent(count("shifter_active"), count("cycles")),
                    count("max_concurrent_shifters"),
                );
                println!(
                    "    shifter overlap: 0 active={} cycles, 1 active={} cycles, 2 active={} cycles",
                    count("active_shifters_0"),
                    count("active_shifters_1"),
                    count("active_shifters_2"),
                );
                if profile.get("controller_execute_cycles").is_some() {
                    let idle_service = count("controller_service_shifters_0");
                    let idle_cfg_reads = count("sram_config_read_shifters_0");
                    println!(
                        "    controller: execute={} cycles, retire={} (shifters idle={}/active={}), service by active shifters 0/1/2={}/{}/{}, event-wait idle/active={}/{}",
                        count("controller_execute_cycles"),
                        count("controller_retire_cycles"),
                        count("controller_retire_shifter_idle_cycles"),
                        count("controller_retire_shifter_active_cycles"),
                        count("controller_service_shifters_0"),
                        count("controller_service_shifters_1"),
                        count("controller_service_shifters_2"),
                        count("controller_event_wait_shifter_idle_cycles"),
                        count("controller_event_wait_shifter_active_cycles"),
                    );
                    if idle_service != 0 {
                        println!(
                            "    idle controller service={} cycles, including {} CFG reads; remaining non-config idle service={} cycles (optimistic target bound)",
                            idle_service,
                            idle_cfg_reads,
                            idle_service.saturating_sub(idle_cfg_reads),
                        );
                    }
                }
                let addresses = profile.get("sram_unique_addresses");
                let unique = |kind: &str| {
                    addresses
                        .and_then(|value| value.get(kind))
                        .and_then(serde_json::Value::as_u64)
                        .unwrap_or(0)
                };
                println!(
                    "    SRAM service: instruction={} cycles/{} addresses; config={} cycles/{} addresses; data={} cycles/{} addresses",
                    count("sram_instruction_read_cycles"),
                    unique("instruction"),
                    count("sram_config_read_cycles"),
                    unique("config"),
                    count("sram_data_read_cycles"),
                    unique("data"),
                );
                let sram_read_cycles = count("sram_instruction_read_cycles")
                    .saturating_add(count("sram_config_read_cycles"))
                    .saturating_add(count("sram_data_read_cycles"));
                let profile_cycles = count("cycles").max(1);
                println!(
                    "    shared SRAM port: {sram_read_cycles}/{} observed read-enable cycles ({}% of run, {} cycles without a read request)",
                    count("cycles"),
                    sram_read_cycles * 100 / profile_cycles,
                    profile_cycles.saturating_sub(sram_read_cycles),
                );
                if profile.get("controller_config_writes").is_some() {
                    println!(
                        "    controller config writes: {}, same-value rewrites={} (candidate redundancy only; side effects not inferred)",
                        count("controller_config_writes"),
                        count("controller_same_value_config_rewrites"),
                    );
                }
                println!(
                    "    FIFO transfers: TX push/pop={}/{}, RX push/pop={}/{}, engine/host TX pushes={}/{}",
                    count("tx_fifo_pushes"),
                    count("tx_fifo_pops"),
                    count("rx_fifo_pushes"),
                    count("rx_fifo_pops"),
                    count("engine_tx_pushes"),
                    count("host_tx_pushes"),
                );
                for channel in 0..2 {
                    println!(
                        "    channel {channel}: TX/RX high-water={}/{}, shifter byte-wait={} cycles (TX FIFO empty={}, while peer active={}, on SRAM data-read={}), RX-full stalls={} cycles",
                        count(&format!("tx_fifo_{channel}_high_water")),
                        count(&format!("rx_fifo_{channel}_high_water")),
                        count(&format!("shifter_{channel}_byte_wait_cycles")),
                        count(&format!("tx_fifo_{channel}_empty_stall_cycles")),
                        count(&format!("tx_fifo_{channel}_empty_stall_peer_active_cycles")),
                        count(&format!("tx_fifo_{channel}_empty_stall_data_read_cycles")),
                        count(&format!("rx_fifo_{channel}_full_stall_cycles")),
                    );
                }
                if profile.get("tx_fifo_any_empty_stall_cycles").is_some() {
                    println!(
                        "    TX starvation overlap: any={} cycles, both={} cycles, a peer shifter active={} cycles, SRAM data-read={} cycles",
                        count("tx_fifo_any_empty_stall_cycles"),
                        count("tx_fifo_both_empty_stall_cycles"),
                        count("tx_fifo_any_empty_stall_peer_active_cycles"),
                        count("tx_fifo_any_empty_stall_data_read_cycles"),
                    );
                }
                let read_push_pairs = count("sram_data_to_tx_push_pairs");
                if read_push_pairs > 0 {
                    println!(
                        "    SRAM data-read to TX FIFO push (controller scheduling interval, not macro latency): pairs={}, unmatched reads={}, min/mean/max={} / {:.1} / {} cycles",
                        read_push_pairs,
                        count("sram_data_read_unpaired_at_end"),
                        count("sram_data_to_tx_push_min_latency_cycles"),
                        count("sram_data_to_tx_push_latency_cycles") as f64
                            / read_push_pairs as f64,
                        count("sram_data_to_tx_push_max_latency_cycles"),
                    );
                    println!(
                        "    request-to-push pairs by channel={}/{}, interval buckets [0,31]/[32,63]/[64,95]/[96,127]/[128,+]={}/{}/{}/{}/{}",
                        count("sram_data_to_tx_push_channel_0_pairs"),
                        count("sram_data_to_tx_push_channel_1_pairs"),
                        count("sram_data_to_tx_push_latency_0_31_cycles"),
                        count("sram_data_to_tx_push_latency_32_63_cycles"),
                        count("sram_data_to_tx_push_latency_64_95_cycles"),
                        count("sram_data_to_tx_push_latency_96_127_cycles"),
                        count("sram_data_to_tx_push_latency_128_plus_cycles"),
                    );
                    if profile
                        .get("tx_fifo_0_empty_stalls_between_data_read_and_push")
                        .is_some()
                    {
                        println!(
                            "    TX-empty stalls between matched data-read and push: ch0={}/{} cycles, ch1={}/{} cycles (numerator is stall cycles inside any matched refill window)",
                            count("tx_fifo_0_empty_stalls_between_data_read_and_push"),
                            count("tx_fifo_0_empty_stall_cycles"),
                            count("tx_fifo_1_empty_stalls_between_data_read_and_push"),
                            count("tx_fifo_1_empty_stall_cycles"),
                        );
                    }
                }
                println!(
                    "    GPIO: {} chip-driven pin-cycles, {} output transitions; per-pin chip drive duty {:?}% and toggles {:?}",
                    count("gpio_driven_pin_cycles"),
                    count("gpio_output_pin_toggles"),
                    (0..8)
                        .map(|pin| {
                            count(&format!("gpio_pin_{pin}_driven_cycles")) * 100
                                / count("cycles").max(1)
                        })
                        .collect::<Vec<_>>(),
                    (0..8)
                        .map(|pin| count(&format!("gpio_pin_{pin}_toggles")))
                        .collect::<Vec<_>>(),
                );
                println!(
                    "    GPIO external input drive: {} pin-cycles; per-pad duty {:?}%",
                    count("external_driven_pin_cycles"),
                    (0..8)
                        .map(|pin| {
                            count(&format!("external_pin_{pin}_driven_cycles")) * 100
                                / count("cycles").max(1)
                        })
                        .collect::<Vec<_>>(),
                );
                let identical_output_pairs = (0..8)
                    .flat_map(|left| (left + 1..8).map(move |right| (left, right)))
                    .filter(|(left, right)| {
                        let overlap = count(&format!("gpio_pair_{left}_{right}_overlap_cycles"));
                        overlap > 0
                            && count(&format!("gpio_pair_{left}_{right}_equal_cycles")) == overlap
                    })
                    .map(|(left, right)| format!("uio{left}=uio{right}"))
                    .collect::<Vec<_>>();
                if !identical_output_pairs.is_empty() {
                    println!(
                        "    GPIO identical output traces while both pads are driven: {:?}",
                        identical_output_pairs,
                    );
                }
                let uart_bytes = count("uart_payload_bytes");
                if uart_bytes > 0 {
                    let cycles = count("cycles").max(1);
                    let endpoints = count("uart_output_endpoints").max(1);
                    println!(
                        "    UART chip-to-peer: {uart_bytes} peer-received bytes, {:.3} payload bits/cycle, {}% average line duty across {endpoints} endpoints",
                        uart_bytes as f64 * 8.0 / cycles as f64,
                        count("uart_line_busy_cycles") * 100 / (cycles * endpoints),
                    );
                }
                if let Some(intervals) = profile
                    .get("gpio_transition_intervals")
                    .and_then(serde_json::Value::as_array)
                {
                    for (pin, values) in intervals.iter().enumerate() {
                        let mut all_values = values
                            .as_array()
                            .into_iter()
                            .flatten()
                            .filter_map(serde_json::Value::as_u64)
                            .collect::<Vec<_>>();
                        if !all_values.is_empty() {
                            all_values.sort_unstable();
                            all_values.dedup();
                            println!(
                                "    GPIO {pin} distinct output-transition intervals (cycles): {:?}",
                                all_values,
                            );
                        }
                    }
                }
                if let Some(records) = profile
                    .get("gpio_transition_records")
                    .and_then(serde_json::Value::as_array)
                {
                    for (pin, values) in records.iter().enumerate() {
                        if pin < 2 {
                            let transitions = values
                                .as_array()
                                .into_iter()
                                .flatten()
                                .take(160)
                                .map(serde_json::Value::to_string)
                                .collect::<Vec<_>>();
                            if !transitions.is_empty() {
                                println!(
                                    "    GPIO {pin} transition trace (cycle, level): {}",
                                    transitions.join(" "),
                                );
                            }
                        }
                    }
                }
                for stream in build.plan["streams"].as_object().into_iter().flatten() {
                    let channel = stream.1["channel"].as_u64().unwrap_or(0);
                    for stage in stream.1["byte_stages"].as_array().into_iter().flatten() {
                        let tile = stage["tile"].as_u64().unwrap_or(0);
                        let kind = stage["kind"].as_str().unwrap_or("unknown");
                        let label = format!("byte_stage_{tile}_{kind}");
                        let metric = |suffix: &str| count(&format!("{label}_{suffix}"));
                        println!(
                            "    byte tile {tile} (channel {channel}, {kind}): input/output transfers={}/{}, output-valid={} cycles, backpressure={} cycles",
                            metric("input_transfers"),
                            metric("output_transfers"),
                            metric("output_valid_cycles"),
                            metric("backpressure_cycles"),
                        );
                    }
                }
                if let Some(transfers) = profile
                    .get("byte_path_transfers")
                    .and_then(serde_json::Value::as_array)
                {
                    println!(
                        "    byte-link transfers: {}",
                        transfers
                            .iter()
                            .map(serde_json::Value::as_u64)
                            .map(|value| value.unwrap_or(0).to_string())
                            .collect::<Vec<_>>()
                            .join(", "),
                    );
                }
            }
            if let Some(values) = profile
                .get("connection_active_cycles")
                .and_then(serde_json::Value::as_array)
            {
                let base_edges = build
                    .signal_connections
                    .iter()
                    .filter(|edge| edge.context.is_none() && edge.line.is_some())
                    .count();
                if values.len() == base_edges {
                    let activity: Vec<_> = values.iter().filter_map(|v| v.as_u64()).collect();
                    if activity.len() == base_edges {
                        for estimate in spatial::estimate_activity(
                            &build.resources,
                            &build.signal_connections,
                            &activity,
                        ) {
                            let cross = estimate
                                .cross_cluster_active_edge_cycles
                                .map_or_else(|| "unknown".to_owned(), |n| n.to_string());
                            let hops = estimate
                                .estimated_active_hop_cycles
                                .map_or_else(|| "unknown".to_owned(), |n| n.to_string());
                            println!(
                                "    {}/{}: cross-cluster active-edge cycles={cross}, estimated active hop-cycles={hops}",
                                estimate.topology, outcome.name,
                            );
                            if estimate.topology.starts_with("graph_search_") {
                                let assignment = estimate
                                    .assignments
                                    .iter()
                                    .map(|(name, cluster)| format!("{name}=cluster{cluster}"))
                                    .collect::<Vec<_>>()
                                    .join(" ");
                                println!(
                                    "      hypothetical trace-weighted assignment: {assignment}"
                                );
                            }
                        }
                    }
                }
            }
            if let Some(values) = profile
                .get("direct_link_activity")
                .and_then(serde_json::Value::as_array)
            {
                let direct_links = build
                    .signal_connections
                    .iter()
                    .filter(|edge| edge.context.is_none() && edge.transport == "direct_link")
                    .count();
                let absorbs: Vec<_> = values
                    .iter()
                    .map(|value| value["absorb"].as_u64().unwrap_or(0))
                    .collect();
                let emits: Vec<_> = values
                    .iter()
                    .map(|value| value["emit"].as_u64().unwrap_or(0))
                    .collect();
                let events: Vec<_> = absorbs.iter().zip(&emits).map(|(a, e)| a + e).collect();
                if direct_links > 0 && events.len() == direct_links {
                    for (index, event) in events.iter().enumerate() {
                        println!(
                            "    direct shifter/CRC link {index}: {} absorbs, {} emits, {event} total events",
                            absorbs[index], emits[index],
                        );
                    }
                    for estimate in spatial::estimate_direct_activity(
                        &build.resources,
                        &build.signal_connections,
                        &events,
                    ) {
                        let cross = estimate
                            .cross_cluster_events
                            .map_or_else(|| "unknown".to_owned(), |n| n.to_string());
                        let hops = estimate
                            .estimated_hop_events
                            .map_or_else(|| "unknown".to_owned(), |n| n.to_string());
                        println!(
                            "    {}/{}: cross-cluster direct-link events={cross}, estimated hop-events={hops}",
                            estimate.topology, outcome.name,
                        );
                    }
                }
            }
            if let Some(values) = profile
                .get("byte_path_transfers")
                .and_then(serde_json::Value::as_array)
            {
                let byte_links = build
                    .signal_connections
                    .iter()
                    .filter(|edge| edge.context.is_none() && edge.transport == "byte_link")
                    .count();
                let transfers: Vec<_> = values.iter().filter_map(|value| value.as_u64()).collect();
                if byte_links > 0 && transfers.len() == byte_links {
                    for estimate in spatial::estimate_byte_activity(
                        &build.resources,
                        &build.signal_connections,
                        &transfers,
                    ) {
                        let cross = estimate
                            .cross_cluster_transfers
                            .map_or_else(|| "unknown".to_owned(), |n| n.to_string());
                        let hops = estimate
                            .estimated_hop_transfers
                            .map_or_else(|| "unknown".to_owned(), |n| n.to_string());
                        println!(
                            "    {}/{}: cross-cluster byte transfers={cross}, estimated hop-transfers={hops}",
                            estimate.topology, outcome.name,
                        );
                    }
                }
            }
            let signal_edges = build
                .signal_connections
                .iter()
                .filter(|edge| edge.context.is_none() && edge.transport == "signal_line")
                .count();
            let signal_activity = profile
                .get("connection_active_cycles")
                .and_then(serde_json::Value::as_array)
                .filter(|values| values.len() == signal_edges)
                .map(|values| {
                    values
                        .iter()
                        .map(|v| v.as_u64().unwrap_or(0))
                        .collect::<Vec<_>>()
                });
            let byte_edges = build
                .signal_connections
                .iter()
                .filter(|edge| edge.context.is_none() && edge.transport == "byte_link")
                .count();
            let byte_activity = profile
                .get("byte_path_transfers")
                .and_then(serde_json::Value::as_array)
                .filter(|values| values.len() == byte_edges)
                .map(|values| {
                    values
                        .iter()
                        .map(|v| v.as_u64().unwrap_or(0))
                        .collect::<Vec<_>>()
                });
            let direct_edges = build
                .signal_connections
                .iter()
                .filter(|edge| edge.context.is_none() && edge.transport == "direct_link")
                .count();
            let direct_activity = profile
                .get("direct_link_activity")
                .and_then(serde_json::Value::as_array)
                .filter(|values| values.len() == direct_edges)
                .map(|values| {
                    values
                        .iter()
                        .map(|v| {
                            v["absorb"].as_u64().unwrap_or(0) + v["emit"].as_u64().unwrap_or(0)
                        })
                        .collect::<Vec<_>>()
                });
            if signal_activity.is_some() || byte_activity.is_some() || direct_activity.is_some() {
                for estimate in spatial::estimate_combined_activity(
                    &build.resources,
                    &build.signal_connections,
                    signal_activity.as_deref(),
                    byte_activity.as_deref(),
                    direct_activity.as_deref(),
                ) {
                    if estimate.topology == "flat64" {
                        continue;
                    }
                    println!(
                        "    {}/{} equal-class normalized cross-hop cost={}; signal cycles={:?}, byte transfers={:?}, direct events={:?}",
                        estimate.topology,
                        outcome.name,
                        estimate.normalized_cross_cost.unwrap_or(0),
                        estimate.signal_cross_cycles,
                        estimate.byte_cross_transfers,
                        estimate.direct_cross_events,
                    );
                    if estimate.topology.starts_with("graph_search_") {
                        let assignment = estimate
                            .assignments
                            .iter()
                            .map(|(name, cluster)| format!("{name}=cluster{cluster}"))
                            .collect::<Vec<_>>()
                            .join(" ");
                        println!("      combined trace-weighted assignment: {assignment}");
                    }
                }
                for (scenario, class_weights, present) in [
                    (
                        "signal-heavy ×10",
                        [10, 1, 1],
                        signal_activity.as_ref().is_some_and(|v| !v.is_empty()),
                    ),
                    (
                        "byte-heavy ×10",
                        [1, 10, 1],
                        byte_activity.as_ref().is_some_and(|v| !v.is_empty()),
                    ),
                    (
                        "direct-heavy ×10",
                        [1, 1, 10],
                        direct_activity.as_ref().is_some_and(|v| !v.is_empty()),
                    ),
                ] {
                    if !present {
                        continue;
                    }
                    for estimate in spatial::estimate_combined_activity_weighted(
                        &build.resources,
                        &build.signal_connections,
                        signal_activity.as_deref(),
                        byte_activity.as_deref(),
                        direct_activity.as_deref(),
                        class_weights,
                    ) {
                        if !estimate.topology.starts_with("graph_search_") {
                            continue;
                        }
                        let assignment = estimate
                            .assignments
                            .iter()
                            .map(|(name, cluster)| format!("{name}=cluster{cluster}"))
                            .collect::<Vec<_>>()
                            .join(" ");
                        println!(
                            "      {scenario} {} cost={} assignment: {assignment}",
                            estimate.topology,
                            estimate.normalized_cross_cost.unwrap_or(0),
                        );
                    }
                }
            }
            let pattern_activity = profile
                .get("connection_asserted_patterns")
                .and_then(serde_json::Value::as_array)
                .and_then(|rows| {
                    rows.iter()
                        .map(|row| {
                            let active = row["active_edges"].as_array()?;
                            let cycles = row["cycles"].as_u64()?;
                            let active = active
                                .iter()
                                .map(serde_json::Value::as_u64)
                                .collect::<Option<Vec<_>>>()?
                                .into_iter()
                                .map(usize::try_from)
                                .collect::<Result<Vec<_>, _>>()
                                .ok()?;
                            Some((active, cycles))
                        })
                        .collect::<Option<Vec<_>>>()
                });
            let signal_edges: Vec<_> = build
                .signal_connections
                .iter()
                .filter(|edge| edge.context.is_none() && edge.transport == "signal_line")
                .cloned()
                .collect();
            let truncated_pattern_cycles = profile
                .get("connection_asserted_patterns_truncated_cycles")
                .and_then(serde_json::Value::as_u64)
                .unwrap_or(0);
            if truncated_pattern_cycles > 0 {
                println!(
                    "    {}: asserted-signal overlap histogram omitted {truncated_pattern_cycles} cycles after reaching its pattern limit",
                    outcome.name,
                );
            }
            if let Some(patterns) = pattern_activity.filter(|_| {
                truncated_pattern_cycles == 0
                    && profile
                        .get("connection_active_cycles")
                        .and_then(serde_json::Value::as_array)
                        .is_some_and(|values| values.len() == signal_edges.len())
            }) {
                for estimate in spatial::estimate_concurrent_interconnect_demand(
                    &build.resources,
                    &signal_edges,
                    &patterns,
                    &[1, 2, 4, 8],
                ) {
                    let overloads = estimate
                        .cycles_with_asserted_demand_over
                        .iter()
                        .map(|(capacity, cycles)| format!("{capacity}:{cycles}"))
                        .collect::<Vec<_>>()
                        .join(", ");
                    println!(
                        "    {}/{} asserted-signal overlap: max bundle={} asserted nets, asserted net-cycles={}, cycles above asserted-width thresholds [lanes:cycles]=[{overloads}]",
                        estimate.topology,
                        outcome.name,
                        estimate.maximum_simultaneous_asserted_nets,
                        estimate.asserted_link_lane_cycles,
                    );
                }
            }
        }
    }
    let failed: Vec<_> = outcomes
        .iter()
        .filter_map(|o| o.error.as_ref().map(|error| (o, error)))
        .collect();
    let reason = |error: &simulator::Failure| {
        format!(
            "{}:{}: {}",
            project.display_path(),
            error.line,
            error.message
        )
    };
    if only.is_some() {
        for (_, error) in &failed {
            println!("\n  {}", reason(error));
        }
        return failed.is_empty();
    }
    let passed = outcomes.len() - failed.len();
    match failed.len() {
        0 => println!("\n  {passed} passed"),
        n => println!("\n  {passed} passed, {n} failed"),
    }
    if failed.is_empty() {
        return true;
    }
    if let [(_, error)] = failed[..] {
        println!("\n  {}", reason(error));
    } else if terminal {
        println!("\n  To see why a test failed, run it on its own:");
        for (outcome, _) in &failed {
            let mut command = format!("spatial run --test={}", shell_quote(&outcome.name));
            if let Some(file) = file {
                command = format!("{command} {}", shell_quote(&file.display().to_string()));
            }
            println!("    {}", paint(on, "36", &command));
        }
    } else {
        for (outcome, error) in &failed {
            println!("\n  {}\n    {}", outcome.name, reason(error));
        }
    }
    false
}

fn percent(part: u64, total: u64) -> u64 {
    part.saturating_mul(100)
        .checked_div(total)
        .unwrap_or_default()
}

// MLIR string literals use hexadecimal byte escapes, including for quotes,
// backslashes and non-ASCII UTF-8 bytes. Never interpolate unescaped source.
fn mlir_string(value: &str) -> String {
    let mut out = String::from("\"");
    for byte in value.bytes() {
        if (0x20..=0x7e).contains(&byte) && byte != b'"' && byte != b'\\' {
            out.push(char::from(byte));
        } else {
            out.push_str(&format!("\\{byte:02X}"));
        }
    }
    out.push('"');
    out
}

fn frontend_mlir(program: &spatial::LogicalProgram, source_name: &str) -> AppResult<String> {
    let protocols = serde_json::to_string(&program.protocols)?;
    let limitations = serde_json::to_string(&program.limitations)?;
    let mut out = String::from("module {\n  \"spatial.frontend_program\"() ({\n");
    for item in &program.items {
        let value = serde_json::to_value(item)?;
        let kind = value["kind"]
            .as_str()
            .ok_or_else(|| io::Error::other("frontend item lacks a kind"))?;
        out.push_str(&format!(
            "    \"spatial.frontend_item\"() {{kind = {}, item_json = {}}} : () -> ()\n",
            mlir_string(kind),
            mlir_string(&serde_json::to_string(&value)?)
        ));
    }
    for protocol in &program.protocols {
        let inputs: Vec<String> = protocol
            .inputs
            .iter()
            .map(|input| match input {
                spatial::LogicalSignal::Constant { value } => {
                    mlir_string(if *value { "constant:1" } else { "constant:0" })
                }
                spatial::LogicalSignal::Resource { name, output } => {
                    let suffix = output
                        .as_ref()
                        .map_or(String::new(), |out| format!(".{out}"));
                    mlir_string(&format!("resource:{name}{suffix}"))
                }
            })
            .collect();
        let rows: Vec<String> = protocol
            .transitions
            .iter()
            .map(|row| {
                let mask = row
                    .inputs
                    .iter()
                    .enumerate()
                    .fold(0usize, |acc, (i, bit)| acc | (usize::from(*bit) << i));
                format!(
                    "{{from = {} : i64, input_mask = {} : i64, to = {} : i64, action = {} : i64}}",
                    row.from,
                    mask,
                    row.to,
                    row.action.unwrap_or(0)
                )
            })
            .collect();
        out.push_str(&format!(
            "    \"spatial.protocol\"() {{name = {}, states = {} : i64, initial_state = {} : i64, inputs = [{}], rows = [{}], protocol_json = {}}} : () -> ()\n",
            mlir_string(&protocol.name),
            protocol.states,
            protocol.initial_state,
            inputs.join(", "),
            rows.join(", "),
            mlir_string(&serde_json::to_string(protocol)?)
        ));
    }
    let graph = serde_json::to_value(program.connection_graph())?;
    let inputs = graph["inputs"]
        .as_array()
        .ok_or_else(|| io::Error::other("frontend connection graph lacks inputs"))?;
    for input in inputs {
        let consumer = input["consumer"]
            .as_str()
            .ok_or_else(|| io::Error::other("frontend input lacks a consumer"))?;
        let port = input["port"]
            .as_str()
            .ok_or_else(|| io::Error::other("frontend input lacks a port"))?;
        out.push_str(&format!(
            "    \"spatial.frontend_input\"() {{consumer = {}, port = {}, input_json = {}}} : () -> ()\n",
            mlir_string(consumer),
            mlir_string(port),
            mlir_string(&serde_json::to_string(input)?)
        ));
    }
    let streams = graph["streams"]
        .as_array()
        .ok_or_else(|| io::Error::other("frontend connection graph lacks streams"))?;
    for stream in streams {
        let name = stream["name"]
            .as_str()
            .ok_or_else(|| io::Error::other("frontend stream lacks a name"))?;
        out.push_str(&format!(
            "    \"spatial.frontend_stream\"() {{name = {}, stream_json = {}}} : () -> ()\n",
            mlir_string(name),
            mlir_string(&serde_json::to_string(stream)?)
        ));
    }
    out.push_str(&format!(
        "  }}) {{source = {}, schema = {}, clock_hz = {}, protocols_json = {}, limitations_json = {}, connection_limitations_json = {}}} : () -> ()\n}}\n",
        mlir_string(source_name),
        mlir_string(program.schema),
        mlir_string(&program.clock_hz),
        mlir_string(&protocols),
        mlir_string(&limitations),
        mlir_string(&serde_json::to_string(&graph["limitations"])?),
    ));
    Ok(out)
}

fn run() -> AppResult<bool> {
    match Cli::parse().command {
        Command::FrontendMlir { file } => {
            let source = fs::read_to_string(&file)?;
            let program = spatial::extract_logical_program(&source).map_err(|error| {
                Reported(error.render(&file.display().to_string(), &source, false))
            })?;
            print!("{}", frontend_mlir(&program, &file.to_string_lossy())?);
        }
        Command::TestPlan { file } => {
            let source = fs::read_to_string(&file)?;
            let full = spatial::test_plan(&source).map_err(|error| {
                Reported(error.render(&file.display().to_string(), &source, false))
            })?;
            let mut plan = serde_json::Map::new();
            for key in ["clock_hz", "pins", "calls", "tests", "invariants"] {
                plan.insert(key.into(), full[key].clone());
            }
            println!(
                "{}",
                serde_json::to_string_pretty(&serde_json::Value::Object(plan))?
            );
        }
        Command::ModelIr { file } => {
            let source = fs::read_to_string(&file)?;
            let program = spatial::extract_logical_program(&source).map_err(|error| {
                Reported(error.render(&file.display().to_string(), &source, false))
            })?;
            let mut document = serde_json::to_value(&program)?;
            document["requirements"] = serde_json::to_value(program.requirements())?;
            document["connections"] = serde_json::to_value(program.connection_graph())?;
            // Each protocol's Mealy machine with its reachability and state
            // classes, for backends that lower protocols themselves.
            let protocols = spatial::extract_logical_protocols(&source).map_err(|error| {
                Reported(error.render(&file.display().to_string(), &source, false))
            })?;
            document["protocols"] = protocols
                .iter()
                .map(|protocol| {
                    let mut value = serde_json::to_value(protocol)?;
                    value["analysis"] =
                        serde_json::to_value(protocol.analyze().map_err(io::Error::other)?)?;
                    Ok::<_, io::Error>(value)
                })
                .collect::<Result<Vec<_>, _>>()?
                .into();
            println!("{}", serde_json::to_string_pretty(&document)?);
        }
        Command::ModelProtocol {
            file,
            max_states,
            max_inputs,
            max_action,
            response_cycles,
            response_deadline,
        } => {
            let source = fs::read_to_string(&file)?;
            let protocols = spatial::extract_logical_protocols(&source).map_err(|error| {
                Reported(error.render(&file.display().to_string(), &source, false))
            })?;
            let candidate = spatial::ProtocolCandidate {
                max_states,
                max_inputs,
                max_action,
                response_cycles,
                response_deadline_cycles: response_deadline,
            };
            let results: Vec<_> = protocols
                .iter()
                .map(|protocol| {
                    let assessment = protocol.assess(&candidate).map_err(io::Error::other)?;
                    Ok::<_, io::Error>(
                        serde_json::json!({"protocol": protocol.name, "assessment": assessment}),
                    )
                })
                .collect::<Result<_, _>>()?;
            println!(
                "{}",
                serde_json::to_string_pretty(&serde_json::json!({
                    "schema": "spatial.protocol-candidate-assessment.v1",
                    "candidate": {"max_states": max_states, "max_inputs": max_inputs,
                        "max_action": max_action, "response_cycles": response_cycles,
                        "response_deadline_cycles": response_deadline},
                    "limitations": "Per-machine capacity and optional response latency only; no signal validation, placement, routing, interaction, throughput, or RTL equivalence proof.",
                    "results": results,
                }))?
            );
        }
        Command::ModelGraph { file } => {
            let source = fs::read_to_string(&file)?;
            let build = spatial::compile(&source).map_err(|error| {
                Reported(error.render(&file.display().to_string(), &source, false))
            })?;
            println!(
                "{}",
                serde_json::to_string_pretty(&build.architecture_manifest())?
            );
        }
        Command::ModelMemory { file } => {
            let experiment: spatial::MemoryExperiment = serde_json::from_slice(&fs::read(file)?)?;
            let results = experiment.evaluate().map_err(std::io::Error::other)?;
            println!(
                "{}",
                serde_json::to_string_pretty(&serde_json::json!({
                    "model": "abstract_memory_timing_v1",
                    "limitations": "Explicit request graph only; not an RTL replay or correctness, area, or physical timing proof.",
                    "candidates": results,
                }))?
            );
        }
        Command::ModelStream {
            cells,
            visible_lines,
            row_repeat,
            independent_data_lanes,
            data_pads,
            sideband_pads,
            total_pads,
            cell_cycles,
            horizontal_blank,
            frame_lines,
            host_cycles_per_byte,
            fifo_depth,
            concurrent_host_bytes,
        } => {
            let model = spatial::VectorStreamModel::evaluate(spatial::VectorStreamConfig {
                cells_per_line: cells,
                visible_lines,
                row_repeat,
                independent_data_lanes,
                data_pads,
                sideband_pads,
                total_pads,
                cell_cycles,
                horizontal_blank_cycles: horizontal_blank,
                frame_lines,
                host_cycles_per_byte,
                fifo_depth_bytes: fifo_depth,
                concurrent_host_bytes_per_frame: concurrent_host_bytes,
            })
            .map_err(std::io::Error::other)?;
            let host_capacity = model.frame_cycles as f64 / host_cycles_per_byte as f64;
            println!(
                "Periodic vector stream: {} output bytes/frame, {} frame cycles",
                model.output_bytes_per_frame, model.frame_cycles
            );
            println!(
                "Payload: {} independent data lane(s)/cell, {} useful bits/frame, {:.4} active bits/cycle, GPIO pads {}/{} ({:.1}%)",
                independent_data_lanes,
                model.useful_data_bits_per_frame,
                independent_data_lanes as f64 / cell_cycles as f64,
                model.pads_used,
                model.total_pads,
                100.0 * model.pads_used as f64 / model.total_pads as f64,
            );
            println!(
                "Primary-stream-only FIFO phase sweep (excludes competing host bytes): worst underflows={}, passing phases={}/{}, (one byte every {} cycles; full FIFO preload).",
                model.direct_fifo_worst_underflows,
                model.direct_fifo_passing_phases,
                model.direct_fifo_phases_swept,
                host_cycles_per_byte,
            );
            println!(
                "Remaining estimates are analytic: no channel-switch cost, instruction traffic, mapped resource contention, or physical timing model."
            );
            for candidate in [&model.direct_fifo, &model.single_row, &model.ping_pong] {
                println!(
                    "  {:<11} host={:>6} primary + {:>6} other = {:>6}/{:>7.1} B/frame ({:>5.1}%), frame_fit={}, local R/W={:>6}/{:>5}, storage={:>4} B, port R/W={:>4.1}/{:>4.1}%, primary_only_window_slack={:>6} cycles",
                    candidate.name,
                    candidate.primary_host_bytes_per_frame,
                    concurrent_host_bytes,
                    candidate.host_bytes_per_frame,
                    host_capacity,
                    100.0 * candidate.host_bytes_per_frame as f64 / host_capacity,
                    if candidate.host_bytes_per_frame as f64 <= host_capacity {
                        "yes"
                    } else {
                        "no"
                    },
                    candidate.local_reads_per_frame,
                    candidate.local_writes_per_frame,
                    candidate.storage_bytes,
                    100.0 * candidate.local_reads_per_frame as f64 / model.frame_cycles as f64,
                    100.0 * candidate.local_writes_per_frame as f64 / model.frame_cycles as f64,
                    candidate.refill_slack_cycles,
                );
            }
        }
        Command::New { name } => {
            project::new(&name)?;
            println!(
                "Created {}/{}\n\n  cd {}\n  spatial run --test",
                name.display(),
                project::SOURCE,
                shell_quote(&name.display().to_string())
            );
        }
        Command::Check { file, physical_map } => {
            let project = Project::find(file.as_deref())?;
            let compiled = build(&project)?;
            if let Some(path) = physical_map {
                let bytes = fs::read(&path)?;
                let placement: spatial::PhysicalPlacementMap = serde_json::from_slice(&bytes)?;
                let estimate = spatial::estimate_physical_locality(
                    &compiled.resources,
                    &compiled.pins,
                    &compiled.signal_connections,
                    &placement,
                );
                println!(
                    "\nPhysical placement proxy: {} mapped edges, {} unlocated edges, {:.3} µm total Manhattan distance, {:.3} µm maximum edge",
                    estimate.mapped_edges,
                    estimate.unmapped_edges,
                    estimate.total_manhattan_um,
                    estimate.maximum_edge_um,
                );
                println!(
                    "  Coordinates are placed-state centroids and GPIO terminals; pin endpoints are scored only for pin.drive on uio_out. These are not routed lengths or timing results."
                );
                for edge in estimate.edges.iter().take(12) {
                    println!(
                        "  {:>9.3} µm  {} -> {}.{} ({})",
                        edge.manhattan_um,
                        edge.source,
                        edge.destination,
                        edge.input,
                        edge.transport,
                    );
                }
                let timer_candidates = if estimate.mapped_edges == 0 {
                    Vec::new()
                } else {
                    spatial::search_physical_index_assignments(
                        &compiled.resources,
                        &compiled.pins,
                        &compiled.signal_connections,
                        &placement,
                        "timer",
                    )
                };
                if !timer_candidates.is_empty() {
                    println!(
                        "  Timer-index candidates (same resource count, unique physical slots):"
                    );
                    for candidate in timer_candidates.iter().take(5) {
                        let assignment = candidate
                            .assignments
                            .iter()
                            .map(|(name, index)| format!("{name}=timer{index}"))
                            .collect::<Vec<_>>()
                            .join(" ");
                        println!(
                            "    total={:.3} µm max={:.3} µm mapped={} unlocated={}  {}",
                            candidate.estimate.total_manhattan_um,
                            candidate.estimate.maximum_edge_um,
                            candidate.estimate.mapped_edges,
                            candidate.estimate.unmapped_edges,
                            assignment,
                        );
                    }
                    let best = &timer_candidates[0];
                    let overrides: std::collections::BTreeMap<_, _> =
                        best.assignments.iter().cloned().collect();
                    let candidate_build =
                        spatial::compile_with_resource_indices(&project.text, &overrides).map_err(
                            |error| {
                                Reported(error.render(
                                    &project.display_path(),
                                    &project.text,
                                    color(io::stderr().is_terminal()),
                                ))
                            },
                        )?;
                    let changed_image_bytes = compiled
                        .image
                        .iter()
                        .zip(&candidate_build.image)
                        .filter(|(left, right)| left != right)
                        .count()
                        + compiled.image.len().abs_diff(candidate_build.image.len());
                    let timers = candidate_build
                        .resources
                        .iter()
                        .filter(|resource| resource.kind == "timer")
                        .map(|resource| format!("{}=timer{}", resource.name, resource.index))
                        .collect::<Vec<_>>()
                        .join(" ");
                    println!(
                        "  Best timer candidate compiles: {timers}; image {} -> {} bytes, {changed_image_bytes} byte positions differ from baseline.",
                        compiled.image.len(),
                        candidate_build.image.len(),
                    );
                    let candidate_estimate = spatial::estimate_physical_locality(
                        &candidate_build.resources,
                        &candidate_build.pins,
                        &candidate_build.signal_connections,
                        &placement,
                    );
                    println!(
                        "  Recompiled candidate score: {:.3} µm, {} mapped / {} unlocated edges.",
                        candidate_estimate.total_manhattan_um,
                        candidate_estimate.mapped_edges,
                        candidate_estimate.unmapped_edges,
                    );
                }
            }
        }
        Command::Build { file } => {
            let project = Project::find(file.as_deref())?;
            let build = build(&project)?;
            fs::create_dir_all(project.target())?;
            let image = project.target().join("image.bin");
            fs::write(&image, &build.image)?;
            println!("  wrote  {}", image.display());
        }
        Command::Run {
            test,
            physical_map,
            file,
        } => {
            let project = Project::find(file.as_deref())?;
            let build = if let Some(path) = physical_map.as_deref() {
                build_best_physical_candidate(&project, path)?
            } else {
                build(&project)?
            };
            println!();
            if test.is_none() {
                console::run(&project, &build)?;
                return Ok(true);
            }
            let only = test.as_deref().filter(|name| !name.is_empty());
            if let Some(name) = only.filter(|name| !build.tests.iter().any(|t| t == name)) {
                return Err(format!(
                    "no test named \"{name}\"; tests: {}",
                    build.tests.join(", ")
                )
                .into());
            }
            if build.tests.is_empty() {
                return Err("this program has no tests; add `test \"name\" { ... }`".into());
            }
            let animation = preview::Animation::start("spatial run --test", "running on the RTL");
            let outcomes = simulator::run(&project.target(), &build.plan, only, &|s| {
                animation.status(s)
            });
            drop(animation);
            let outcomes = outcomes?;
            return Ok(report(&project, file.as_deref(), &build, &outcomes, only));
        }
        Command::Preview { snapshot } => {
            if snapshot {
                println!("{}", preview::snapshot());
            } else {
                preview::run()?;
            }
        }
    }
    Ok(true)
}

fn main() {
    match run() {
        Ok(true) => {}
        Ok(false) => std::process::exit(1),
        Err(error) => {
            match error.downcast_ref::<Reported>() {
                Some(Reported(text)) => eprintln!("{text}"),
                None => {
                    let on = color(io::stderr().is_terminal());
                    eprintln!("{}: {error}", paint(on, "1;31", "error"));
                }
            }
            std::process::exit(1);
        }
    }
}
