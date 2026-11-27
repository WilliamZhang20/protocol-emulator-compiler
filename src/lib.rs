//! Compile Spatial programs (`.spt`) for the PX8 spatial protocol chip.
//!
//! A program wires the chip's hardware tiles (pins, timers, shifters,
//! counters, logic cells) into a protocol, exports streams the host can use,
//! and carries its own tests:
//!
//! ```
//! let build = spatial::compile(r#"
//!     pin led: out = blink.out
//!     timer blink { every: 1 us, run: 1 }
//! "#)?;
//! assert_eq!(build.pins["led"], 0);
//! assert!(build.image.len() <= 1024);
//! # Ok::<(), spatial::Error>(())
//! ```
//!
//! Everything is checked against what the chip actually has: a program that
//! needs a fifth timer, a ninth pin, or a duration that is not a whole number
//! of clock cycles is rejected with an [`Error`] pointing at the source.

mod architecture;
mod byte_pipeline;
mod control;
mod diagnostic;
mod elaborate;
mod hardware;
mod logic;
mod logical_graph;
mod memory_model;
mod plan;
mod program_ir;
pub mod protocol;
mod protocol_ir;
mod px8;
mod stream_model;
mod syntax;
mod transfer_ir;
mod value;

pub use diagnostic::{Error, Span};
use std::collections::BTreeMap;

/// The spatial RTL revision whose register layout this compiler targets.
pub const HARDWARE_REVISION: &str = "7dd2f41c3cc3065129ad6ee69599057834ad5f28";

/// A compiled program.
#[derive(Debug)]
pub struct Build {
    /// SRAM image: controller code followed by configuration tables.
    pub image: Vec<u8>,
    /// Exact shared-SRAM layout and stored-transfer lowering decisions.
    pub memory_layout: ProgramMemoryLayout,
    /// Physical `uio` pad assigned to each pin.
    pub pins: BTreeMap<String, usize>,
    /// Hardware used, as `(name, used, available)`; `memory` is in bytes.
    pub usage: Vec<(&'static str, usize, usize)>,
    /// Named hardware allocations and the signal-space lines they provide.
    pub resources: Vec<ResourceAssignment>,
    /// Configured signal-space edges found in the base image and contexts.
    pub signal_connections: Vec<SignalConnection>,
    /// First-pass topology estimates from the static signal graph.
    pub topology_estimates: Vec<TopologyEstimate>,
    /// Protocol graphs, chosen actor implementations, and mapping estimates.
    pub protocol_mappings: Vec<ProtocolMapping>,
    /// Logical protocol state machines, before actor and signal allocation.
    pub logical_protocols: Vec<LogicalProtocol>,
    /// Source-level program before target resource allocation.
    pub logical_program: LogicalProgram,
    /// Actor placement candidates considered by the protocol mapper.
    pub protocol_placement_search: ProtocolPlacementSearch,
    /// Names of the program's tests, in source order.
    pub tests: Vec<String>,
    /// Everything the simulator needs to run the tests, as JSON.
    pub plan: serde_json::Value,
}

impl Build {
    /// Export the partial graph extracted from this compilation, with explicit
    /// limitations. A null source is an unresolved producer, not a constant.
    pub fn architecture_manifest(&self) -> serde_json::Value {
        let resources: Vec<_> = self
            .resources
            .iter()
            .map(|r| {
                serde_json::json!({
                    "name": r.name, "kind": r.kind, "index": r.index,
                    "config": r.config, "outputs": r.outputs,
                })
            })
            .collect();
        let edges: Vec<_> = self
            .signal_connections
            .iter()
            .map(|e| {
                serde_json::json!({
                    "source": e.source, "destination": e.destination, "input": e.input,
                    "transport": e.transport, "line": e.line, "source_index": e.source_index,
                    "context": e.context,
                })
            })
            .collect();
        let unresolved = self
            .signal_connections
            .iter()
            .filter(|e| e.source.is_none())
            .count();
        serde_json::json!({
            "schema": "spatial.partial-architecture-graph.v1",
            "hardware_revision": HARDWARE_REVISION,
            "actor_abi_required": self.plan.get("actor_abi"),
            "graph_complete": false,
            "unresolved_producer_edges": unresolved,
            "limitations": [
                "This is a partial configured-resource graph, not a complete dataflow graph.",
                "Constants and unrepresented dependencies are omitted; null sources are unresolved.",
                "Context-specific selectors are overrides, not independent concurrent execution.",
                "Memory requests, resource activity, arbitration, and physical placement are not inferred."
            ],
            "resources": resources, "edges": edges, "pins": self.pins,
            "program_memory": self.memory_layout,
            "logical_protocols": self.logical_protocols,
            "logical_program": self.logical_program,
            "logical_requirements": self.logical_program.requirements(),
            "logical_connections": self.logical_program.connection_graph(),
        })
    }
}

pub use architecture::{
    ActivityTopologyEstimate, ByteActivityTopologyEstimate, CombinedActivityTopologyEstimate,
    ConcurrentInterconnectEstimate, DirectActivityTopologyEstimate, InterconnectWidthEstimate,
    PhysicalEdgeEstimate, PhysicalIndexCandidate, PhysicalLocalityEstimate, PhysicalPlacementMap,
    SignalConnection, TopologyEstimate, estimate_activity, estimate_byte_activity,
    estimate_combined_activity, estimate_combined_activity_weighted,
    estimate_concurrent_interconnect_demand, estimate_direct_activity,
    estimate_interconnect_widths, estimate_physical_locality, search_physical_index_assignments,
};
pub use logical_graph::{
    LogicalCheckStatus, LogicalConnectionGraph, LogicalFormulaSources, LogicalInputCheck,
    LogicalInputConnection, LogicalReference, LogicalSignalKind, LogicalStreamConnection,
};
pub use memory_model::{
    MemoryAccessResult, MemoryCandidate, MemoryCandidateResult, MemoryExperiment, MemoryRequest,
};
pub use program_ir::{
    LogicalArgument, LogicalCycles, LogicalExpr, LogicalExprKind, LogicalField, LogicalItem,
    LogicalOperation, LogicalOperationKind, LogicalProgram, LogicalRequirements, LogicalStage,
    LogicalType, LogicalWidth, RuntimeFrameUpdate, TimerTiming, WidthRequirement,
    extract_logical_program,
};
pub use protocol_ir::{
    LogicalProtocol, LogicalSignal, LogicalTransition, ProtocolAnalysis, ProtocolCandidate,
    ProtocolCandidateAssessment, ProtocolStep, extract_logical_protocols,
};
pub use stream_model::{VectorStreamCandidate, VectorStreamConfig, VectorStreamModel};
pub use transfer_ir::{
    MemoryObject, ProgramMemoryLayout, StoredStream, StoredTransfer, TransferPhase,
};

/// A source-level resource's physical location in the target tile.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct ResourceAssignment {
    /// Source name, or a generated name for an implicit resource such as an edge cell.
    pub name: String,
    /// Physical resource class, such as `timer` or `logic cell`.
    pub kind: &'static str,
    /// Physical tile index; pin cells use the actual pad number.
    pub index: usize,
    /// First configuration address for this resource, when it has one.
    pub config: Option<u8>,
    /// Named signal-space outputs as `(output name, line number)`.
    pub outputs: Vec<(&'static str, u8)>,
}

/// A mapped protocol-control graph and the metrics available to the oracle.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct ProtocolMapping {
    /// Source graph name.
    pub name: String,
    /// Chosen implementation: spatial actor or PX8 controller.
    pub implementation: &'static str,
    /// Physical protocol actor, 0 or 1, when the actor backend is selected.
    pub actor: Option<usize>,
    /// Whether a spatial actor uses 8 states with one input instead of 4 states with two.
    pub wide_state: bool,
    /// Selected input line in the 64-line fabric.
    pub input_line: u8,
    /// Optional second selected input line for a two-condition actor.
    pub input_line2: Option<u8>,
    /// Source states before minimization.
    pub source_states: usize,
    /// States left after unreachable-state removal and minimization.
    pub mapped_states: usize,
    /// Number of source transition rows.
    pub source_transitions: usize,
    /// Explicit transition rows remaining after minimization.
    pub mapped_transitions: usize,
    /// Source states removed or merged by the optimizer.
    pub states_reduced: usize,
    /// Signal connections placed beside the matching shifter channel.
    pub local_channel_connections: usize,
    /// Actor assignments evaluated by the mapper for this allocation.
    pub placement_candidates_evaluated: usize,
    /// Actor pipeline stages from observed input to action output.
    pub actor_pipeline_cycles: Option<usize>,
    /// Best-case PX8 controller service cycles from a pulse to the next wait.
    /// This excludes time waiting for an input event.
    pub controller_service_cycles: Option<usize>,
    /// Minimum spacing between source input pulses, when statically known.
    pub minimum_input_interval_cycles: Option<usize>,
}

/// An implementation or placement option evaluated by the mapper.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct ProtocolPlacementCandidate {
    /// Implementation family evaluated for this candidate.
    pub implementation: &'static str,
    /// Whether the candidate satisfies the graph and target constraints.
    pub feasible: bool,
    /// `(protocol graph, actor index)` assignments in this candidate.
    pub assignments: Vec<(String, usize)>,
    /// Direct input/action signal connections placed beside their shifter.
    pub local_channel_connections: usize,
    /// Protocol actors active concurrently in this implementation.
    pub parallel_actors: usize,
    /// Actor pipeline latency when this option uses spatial actors.
    pub actor_pipeline_cycles: Option<usize>,
    /// Best-case PX8 controller service cycles for an input transition.
    pub controller_service_cycles: Option<usize>,
    /// Minimum spacing between source input pulses, when statically known.
    pub minimum_input_interval_cycles: Option<usize>,
}

/// Search trace for the protocol implementation and actor placement decision.
#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct ProtocolPlacementSearch {
    /// Candidates in deterministic evaluation order.
    pub candidates: Vec<ProtocolPlacementCandidate>,
    /// Index of the selected candidate, when any protocol graph exists.
    pub selected: Option<usize>,
}

/// The program's tests and invariants as a hardware-independent plan
/// (`spatial test-plan`). Constructs that compile only for the reorganised
/// fabric (`machine`, `compare`, a stream's `gate` stage) are accepted: a
/// plan needs no image, so whatever reads them is left unconnected.
///
/// # Errors
/// Returns the first problem found, with the source range it concerns.
pub fn test_plan(source: &str) -> Result<serde_json::Value, Error> {
    let mut items = syntax::parse(source)?;
    let logical_program = program_ir::extract(&items)?;
    for item in &mut items {
        if let syntax::ast::Item::Stream { stages, .. } = item {
            stages.retain(|stage| stage.kind.text != "gate");
        }
    }
    let design = elaborate::elaborate_for_plan(&items, &logical_program.protocols)?;
    plan::plan(&design, source)
}

/// Parse, check and compile a program.
///
/// # Errors
/// Returns the first problem found, with the source range it concerns.
pub fn compile(source: &str) -> Result<Build, Error> {
    compile_with_resource_indices(source, &BTreeMap::new())
}

/// Compile a program while pinning selected declared timers to physical
/// indices. Unspecified timers take the first free index; all other resources
/// retain their default allocation. This supports architecture-search
/// candidates without changing the default mapping.
///
/// # Errors
/// Returns the same source errors as [`compile`], or an error if an override
/// names no hardware resource, targets a non-timer resource, exceeds timer
/// capacity, or collides with another timer.
pub fn compile_with_resource_indices(
    source: &str,
    resource_indices: &BTreeMap<String, usize>,
) -> Result<Build, Error> {
    let items = syntax::parse(source)?;
    let logical_program = program_ir::extract(&items)?;
    let logical_protocols = logical_program.protocols.clone();
    let design =
        elaborate::elaborate_with_resource_indices(&items, resource_indices, &logical_protocols)?;
    let plan = plan::plan(&design, source)?;
    Ok(Build {
        tests: design
            .tests
            .iter()
            .map(|(name, _, _)| name.text.clone())
            .collect(),
        usage: design.usage,
        resources: design.resources,
        signal_connections: design.signal_connections,
        topology_estimates: design.topology_estimates,
        protocol_mappings: design.protocol_mappings,
        logical_protocols,
        logical_program,
        protocol_placement_search: design.protocol_placement_search,
        pins: design.pins,
        image: design.image,
        memory_layout: design.memory_layout,
        plan,
    })
}
