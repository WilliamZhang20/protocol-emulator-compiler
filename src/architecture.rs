//! Static configured-dependency graph and first-pass topology estimates.
//!
//! These estimates count configured connections. They are not activity,
//! wire-length, timing, or congestion measurements.

use crate::ResourceAssignment;
use serde::Deserialize;
use std::collections::{BTreeMap, BTreeSet};

type WeightedEdge<'a> = (&'a SignalConnection, u64, u64);
type TrafficCategory<'a> = (&'a str, Vec<WeightedEdge<'a>>);

/// A configured dependency between two resources, including selector and
/// dedicated byte/CRC links.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct SignalConnection {
    /// Name of the resource that produces this signal, when it has a known
    /// signal-space output.
    pub source: Option<String>,
    /// Signal-space line selected by the consumer.
    pub line: Option<u8>,
    /// Transport carrying this dependency.
    pub transport: &'static str,
    /// Source resource index for direct links that bypass the signal lines.
    pub source_index: Option<usize>,
    /// Name of the resource that consumes the signal.
    pub destination: String,
    /// Consumer input role, such as `timer.start` or `logic.in0`.
    pub input: String,
    /// Context bank that owns this selector, or `None` for the base image.
    pub context: Option<usize>,
}

/// Structural cost estimate for one abstract topology.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct TopologyEstimate {
    /// Stable topology identifier.
    pub topology: &'static str,
    /// Configured edges that stay within one abstract cluster.
    pub local_connections: Option<usize>,
    /// Configured edges that cross abstract clusters.
    pub cross_cluster_connections: Option<usize>,
    /// Sum of estimated Manhattan hops over configured edges.
    pub estimated_hops: Option<usize>,
    /// Hypothetical resource-to-cluster assignment used for this estimate.
    pub assignments: Vec<(String, usize)>,
    /// Assumptions used by this first-pass estimate.
    pub assumptions: &'static str,
}

/// Static demand on each abstract inter-cluster wire bundle. Signal and direct
/// links include shifter-to-CRC control/data and CRC return; byte links include
/// data, tags, valid, and reverse ready. This is a
/// connectivity-capacity model, not a timing or congestion measurement;
/// intra-cluster wiring is outside its scope.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct InterconnectWidthEstimate {
    /// Abstract topology evaluated.
    pub topology: &'static str,
    /// Hypothetical scalar lanes available per directed inter-cluster bundle.
    pub capacity_per_direction: usize,
    /// Sum of routed scalar-lane segments over every directed bundle.
    pub total_intercluster_net_segments: usize,
    /// Largest scalar-lane demand on any single directed bundle.
    pub maximum_bundle_width: usize,
    /// Directed bundles whose required net count exceeds the swept capacity.
    pub over_capacity_bundles: Vec<(String, usize)>,
}

/// Trace-derived asserted-signal density on directed abstract links. Activity
/// means a selected one-bit signal is high that cycle; it is not a physical
/// link-capacity requirement because low-level dependencies still need to be
/// represented by a spatial implementation.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct ConcurrentInterconnectEstimate {
    /// Abstract topology evaluated.
    pub topology: &'static str,
    /// Largest number of distinct asserted signal nets on one directed bundle
    /// in any observed cycle.
    pub maximum_simultaneous_asserted_nets: usize,
    /// Sum of asserted net-cycles across all directed inter-cluster bundles.
    pub asserted_link_lane_cycles: u64,
    /// For each threshold, cycles with an asserted bundle wider than it.
    pub cycles_with_asserted_demand_over: Vec<(usize, u64)>,
}

type DirectedBundle = (usize, usize);

/// Workload-weighted structural estimate from an RTL activity profile.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct ActivityTopologyEstimate {
    /// Stable topology identifier.
    pub topology: &'static str,
    /// Active configured-edge cycles that cross abstract clusters.
    pub cross_cluster_active_edge_cycles: Option<u64>,
    /// Active configured-edge cycles multiplied by abstract hop distance.
    pub estimated_active_hop_cycles: Option<u64>,
    /// Hypothetical resource-to-cluster assignment used for this estimate.
    pub assignments: Vec<(String, usize)>,
}

/// Trace-weighted locality estimate for byte transfers between byte tiles and
/// shifters. Transfer counts stay separate from signal active-cycle counts.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct ByteActivityTopologyEstimate {
    /// Stable topology identifier.
    pub topology: &'static str,
    /// Observed byte transfers crossing abstract cluster boundaries.
    pub cross_cluster_transfers: Option<u64>,
    /// Observed transfers multiplied by abstract hop distance.
    pub estimated_hop_transfers: Option<u64>,
    /// Hypothetical resource-to-cluster assignment used for the estimate.
    pub assignments: Vec<(String, usize)>,
}

/// Trace-weighted locality estimate for shifter/CRC direct-link events.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct DirectActivityTopologyEstimate {
    /// Stable topology identifier.
    pub topology: &'static str,
    /// Observed absorb and emit events crossing abstract cluster boundaries.
    pub cross_cluster_events: Option<u64>,
    /// Observed link events multiplied by abstract hop distance.
    pub estimated_hop_events: Option<u64>,
    /// Hypothetical resource-to-cluster assignment used for the estimate.
    pub assignments: Vec<(String, usize)>,
}

/// Single placement estimate combining normalized activity from each observed
/// communication path. Each present path class contributes equal total
/// weight; raw counts remain separated because their units differ.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct CombinedActivityTopologyEstimate {
    /// Stable topology identifier.
    pub topology: &'static str,
    /// Sum of normalized cross-cluster weights multiplied by abstract hops.
    pub normalized_cross_cost: Option<u64>,
    /// Measured signal-line active cycles crossing clusters.
    pub signal_cross_cycles: Option<u64>,
    /// Measured byte transfers crossing clusters.
    pub byte_cross_transfers: Option<u64>,
    /// Measured direct-link events crossing clusters.
    pub direct_cross_events: Option<u64>,
    /// Hypothetical resource-to-cluster assignment used for this estimate.
    pub assignments: Vec<(String, usize)>,
}

/// Physical coordinates extracted from a placed hardware database. Resource
/// coordinates are indexed by physical resource index; GPIO coordinates are
/// indexed by pad number. These points are placement centroids or interface
/// terminals, not routed-wire locations.
#[derive(Clone, Debug, Deserialize)]
pub struct PhysicalPlacementMap {
    /// Resource class to indexed `(x, y)` centroid coordinates in microns.
    pub resource_centroids_um: BTreeMap<String, Vec<[f64; 2]>>,
    /// GPIO terminal class to indexed `(x, y)` terminal coordinates.
    pub gpio_terminals_um: BTreeMap<String, Vec<[f64; 2]>>,
}

/// One configured compiler edge measured against placed resource coordinates.
#[derive(Clone, Debug, PartialEq)]
pub struct PhysicalEdgeEstimate {
    /// Source resource name.
    pub source: String,
    /// Destination resource name.
    pub destination: String,
    /// Selector input role at the destination.
    pub input: String,
    /// Configured transport carrying the edge.
    pub transport: &'static str,
    /// Manhattan distance between placement centroids or a centroid and GPIO
    /// terminal, in microns.
    pub manhattan_um: f64,
}

/// Geometric locality estimate from the actual physical placement map.
#[derive(Clone, Debug, PartialEq)]
pub struct PhysicalLocalityEstimate {
    /// Configured edges with coordinates at both ends.
    pub mapped_edges: usize,
    /// Edges omitted because a resource lacks a physical coordinate.
    pub unmapped_edges: usize,
    /// Sum of mapped-edge Manhattan distances in microns.
    pub total_manhattan_um: f64,
    /// Longest mapped-edge Manhattan distance in microns.
    pub maximum_edge_um: f64,
    /// Per-edge distances, sorted from longest to shortest.
    pub edges: Vec<PhysicalEdgeEstimate>,
}

/// One legal injective resource-index assignment and its physical cost.
#[derive(Clone, Debug, PartialEq)]
pub struct PhysicalIndexCandidate {
    /// `(resource name, physical index)` assignments for the searched class.
    pub assignments: Vec<(String, usize)>,
    /// Physical-locality estimate under this index assignment.
    pub estimate: PhysicalLocalityEstimate,
}

/// Score every injective index assignment for resources of one compatible
/// class, using the physical slots available in the placement map.
pub fn search_physical_index_assignments(
    resources: &[ResourceAssignment],
    pins: &BTreeMap<String, usize>,
    connections: &[SignalConnection],
    placement: &PhysicalPlacementMap,
    kind: &str,
) -> Vec<PhysicalIndexCandidate> {
    let Some(slot_count) = physical_resource_slots(kind, placement).map(|slots| slots.len()) else {
        return Vec::new();
    };
    let searched: Vec<_> = resources
        .iter()
        .filter(|resource| resource.kind == kind)
        .collect();
    if searched.is_empty() || searched.len() > slot_count {
        return Vec::new();
    }
    let names: Vec<_> = searched
        .iter()
        .map(|resource| resource.name.clone())
        .collect();
    let mut candidates = Vec::new();
    enumerate_index_assignments(
        resources,
        pins,
        connections,
        placement,
        kind,
        &names,
        0,
        &mut BTreeSet::new(),
        &mut Vec::new(),
        &mut candidates,
    );
    candidates.sort_by(|left, right| {
        left.estimate
            .total_manhattan_um
            .total_cmp(&right.estimate.total_manhattan_um)
            .then_with(|| {
                left.estimate
                    .maximum_edge_um
                    .total_cmp(&right.estimate.maximum_edge_um)
            })
            .then_with(|| left.assignments.cmp(&right.assignments))
    });
    candidates
}

/// Estimate the physical distance of every configured edge with two located
/// endpoints. Missing coordinates are counted, never assigned a zero cost.
pub fn estimate_physical_locality(
    resources: &[ResourceAssignment],
    pins: &BTreeMap<String, usize>,
    connections: &[SignalConnection],
    placement: &PhysicalPlacementMap,
) -> PhysicalLocalityEstimate {
    let by_name: BTreeMap<_, _> = resources
        .iter()
        .map(|resource| (resource.name.as_str(), resource))
        .collect();
    let mut edges = Vec::new();
    let mut unmapped_edges = 0;
    for connection in connections {
        let Some(source_name) = connection.source.as_deref() else {
            unmapped_edges += 1;
            continue;
        };
        let (Some(source), Some(destination)) = (
            by_name.get(source_name),
            by_name.get(connection.destination.as_str()),
        ) else {
            unmapped_edges += 1;
            continue;
        };
        // The current Build model does not retain pin direction. Score only
        // an explicitly configured pin.drive destination against uio_out;
        // do not guess whether a pin used as a source belongs to uio_in.
        if source.kind == "pin cell"
            || (destination.kind == "pin cell" && connection.input != "pin.drive")
        {
            unmapped_edges += 1;
            continue;
        }
        let (Some(source_xy), Some(destination_xy)) = (
            physical_resource_coordinate(source, pins, placement),
            physical_resource_coordinate(destination, pins, placement),
        ) else {
            unmapped_edges += 1;
            continue;
        };
        let manhattan_um = ((source_xy[0] - destination_xy[0]).abs()
            + (source_xy[1] - destination_xy[1]).abs())
            * 1000.0;
        let manhattan_um = manhattan_um.round() / 1000.0;
        edges.push(PhysicalEdgeEstimate {
            source: source.name.clone(),
            destination: destination.name.clone(),
            input: connection.input.clone(),
            transport: connection.transport,
            manhattan_um,
        });
    }
    edges.sort_by(|left, right| {
        right
            .manhattan_um
            .total_cmp(&left.manhattan_um)
            .then_with(|| left.source.cmp(&right.source))
            .then_with(|| left.destination.cmp(&right.destination))
            .then_with(|| left.input.cmp(&right.input))
    });
    let total_milli_um: u64 = edges
        .iter()
        .map(|edge| (edge.manhattan_um * 1000.0).round() as u64)
        .sum();
    let total_manhattan_um = total_milli_um as f64 / 1000.0;
    PhysicalLocalityEstimate {
        mapped_edges: edges.len(),
        unmapped_edges,
        total_manhattan_um,
        maximum_edge_um: edges.first().map_or(0.0, |edge| edge.manhattan_um),
        edges,
    }
}

fn physical_resource_slots<'a>(
    kind: &str,
    placement: &'a PhysicalPlacementMap,
) -> Option<&'a [[f64; 2]]> {
    let key = match kind {
        "logic cell" => "logic_cell",
        "edge cell" => "edge_cell",
        "CRC" => "crc",
        other => other,
    };
    placement.resource_centroids_um.get(key).map(Vec::as_slice)
}

fn physical_resource_coordinate(
    resource: &ResourceAssignment,
    pins: &BTreeMap<String, usize>,
    placement: &PhysicalPlacementMap,
) -> Option<[f64; 2]> {
    if resource.kind == "pin cell" {
        let pad = pins.get(&resource.name)?;
        return placement
            .gpio_terminals_um
            .get("uio_out")?
            .get(*pad)
            .copied();
    }
    physical_resource_slots(resource.kind, placement)?
        .get(resource.index)
        .copied()
}

#[allow(clippy::too_many_arguments)]
fn enumerate_index_assignments(
    resources: &[ResourceAssignment],
    pins: &BTreeMap<String, usize>,
    connections: &[SignalConnection],
    placement: &PhysicalPlacementMap,
    kind: &str,
    names: &[String],
    depth: usize,
    used: &mut BTreeSet<usize>,
    assignment: &mut Vec<(String, usize)>,
    candidates: &mut Vec<PhysicalIndexCandidate>,
) {
    if depth == names.len() {
        let mut relocated = resources.to_vec();
        for (name, index) in assignment.iter() {
            if let Some(resource) = relocated
                .iter_mut()
                .find(|resource| resource.name == *name && resource.kind == kind)
            {
                resource.index = *index;
            }
        }
        candidates.push(PhysicalIndexCandidate {
            assignments: assignment.clone(),
            estimate: estimate_physical_locality(&relocated, pins, connections, placement),
        });
        return;
    }
    let Some(slots) = physical_resource_slots(kind, placement) else {
        return;
    };
    for index in 0..slots.len() {
        if used.insert(index) {
            assignment.push((names[depth].clone(), index));
            enumerate_index_assignments(
                resources,
                pins,
                connections,
                placement,
                kind,
                names,
                depth + 1,
                used,
                assignment,
                candidates,
            );
            assignment.pop();
            used.remove(&index);
        }
    }
}

/// Extract signal selectors from the exact register writes emitted for a
/// resource. Only selectors represented by a line produced by another named
/// resource are retained; constants and unowned inputs remain outside this
/// first graph.
pub(crate) fn decode_selectors(
    kind: &str,
    name: &str,
    index: usize,
    writes: &[(u8, u16)],
    context: Option<usize>,
) -> Vec<SignalConnection> {
    let writes: BTreeMap<_, _> = writes.iter().copied().collect();
    let mut edges = Vec::new();
    let mut add = |input: &str, line: u8| {
        if line > 1 && line < 64 {
            edges.push(SignalConnection {
                source: None,
                line: Some(line),
                transport: "signal_line",
                source_index: None,
                destination: name.to_owned(),
                input: input.to_owned(),
                context,
            });
        }
    };
    let get = |address: u8| writes.get(&address).copied().unwrap_or(0);
    match kind {
        "pin" => {
            let value = get(0x18 + index as u8);
            if (value >> 6) & 3 != 0 && value & (1 << 5) == 0 {
                add("pin.drive", 32 + (value & 0x1f) as u8);
            }
        }
        "timer" => {
            let base = 4 * index as u8;
            let control = get(base + 2);
            add("timer.start", (control & 0x3f) as u8);
            add("timer.stop", ((control >> 6) & 0x3f) as u8);
            let sync = get(base + 3);
            if sync & (1 << 6) != 0 {
                add("timer.resync", (sync & 0x3f) as u8);
            }
            if sync & (1 << 12) != 0 {
                add("timer.hold", 32 + ((sync >> 7) & 0x1f) as u8);
            }
        }
        "shifter" => {
            let base = 0x10 + 4 * index as u8;
            let timing = get(base + 2);
            add("shifter.shift", (timing & 0x3f) as u8);
            let sample_at = ((timing >> 6) & 0x3f) as u8;
            add("shifter.sample_at", sample_at);
            let control = get(base + 3);
            add("shifter.start", (control & 0x3f) as u8);
            if control & (1 << 12) != 0 && sample_at != 0 {
                add("shifter.sample_data", 32 + ((control >> 6) & 0x1f) as u8);
            }
            let burst = get(0x32 + index as u8);
            if burst & (1 << 5) != 0 {
                add("shifter.end", 32 + (burst & 0x1f) as u8);
            }
            add("shifter.abort", ((burst >> 7) & 0x3f) as u8);
        }
        "counter" => {
            let base = 0x20 + 4 * index as u8;
            let control = get(base + 1);
            add("counter.count", (control & 0x3f) as u8);
            if (control >> 11) & 3 != 0 {
                add("counter.streak_data", 32 + ((control >> 6) & 0x1f) as u8);
            }
            add("counter.clear", (get(base + 2) & 0x3f) as u8);
        }
        "logic cell" => {
            let base = 0x40 + 4 * index as u8;
            let table = get(base);
            let pulse_inputs = get(base + 1);
            let level_inputs = get(base + 2);
            for slot in 0..4 {
                let used = (0..16).any(|row| {
                    row & (1 << slot) == 0
                        && ((table >> row) & 1) != ((table >> (row | (1 << slot))) & 1)
                });
                if !used {
                    continue;
                }
                let line = if slot < 2 {
                    ((pulse_inputs >> (6 * slot)) & 0x3f) as u8
                } else {
                    32 + ((level_inputs >> (5 * (slot - 2))) & 0x1f) as u8
                };
                add(&format!("logic.in{slot}"), line);
            }
        }
        "crc" => {
            let base = if index == 0 { 0x28 } else { 0x88 };
            let data = get(base + 4);
            add("crc.absorb", (data & 0x3f) as u8);
            if get(base + 6) & (1 << 6) == 0 {
                add("crc.data", 32 + ((data >> 6) & 0x1f) as u8);
            }
            let control = get(base + 5);
            add("crc.emit", (control & 0x3f) as u8);
            add("crc.restart", ((control >> 6) & 0x3f) as u8);
            let gates = get(base + 7);
            if gates & (1 << 5) != 0 {
                add("crc.absorb_while", 32 + (gates & 0x1f) as u8);
            }
            if gates & (1 << 11) != 0 {
                add("crc.emit_while", 32 + ((gates >> 6) & 0x1f) as u8);
            }
            let link = get(base + 6);
            if link & (1 << 6) != 0 {
                edges.push(SignalConnection {
                    source: None,
                    line: None,
                    transport: "direct_link",
                    source_index: Some(usize::from((link >> 7) & 1)),
                    destination: name.to_owned(),
                    input: "crc.link_data".to_owned(),
                    context,
                });
            }
        }
        "protocol" => {
            let inputs = get(0xa0 + index as u8);
            add("protocol.input", (inputs & 0x3f) as u8);
            add("protocol.input2", ((inputs >> 6) & 0x3f) as u8);
        }
        _ => {}
    }
    edges
}

/// Connect source lines to their known resource producers.
pub(crate) fn connect_producers(
    resources: &[ResourceAssignment],
    mut edges: Vec<SignalConnection>,
) -> Vec<SignalConnection> {
    for edge in &mut edges {
        edge.source = if edge.transport == "byte_link" {
            edge.source.clone()
        } else if let Some(line) = edge.line {
            resources
                .iter()
                .find(|resource| resource.outputs.iter().any(|(_, output)| *output == line))
                .map(|resource| resource.name.clone())
        } else {
            edge.source_index.and_then(|index| {
                resources
                    .iter()
                    .find(|resource| resource.kind == "shifter" && resource.index == index)
                    .map(|resource| resource.name.clone())
            })
        };
    }
    edges
}

/// Compare simple abstract placements using the configured, unweighted edge
/// list. The cluster choices are hypotheses for exploration, not placements
/// fed to the physical tools.
pub(crate) fn estimate_topologies(
    resources: &[ResourceAssignment],
    edges: &[SignalConnection],
) -> Vec<TopologyEstimate> {
    // Context writes are alternatives to the base configuration, not work
    // performed in addition to it. Model the base image here and retain the
    // contextual selectors in the exported graph for a later per-context pass.
    let base_edges: Vec<_> = edges.iter().filter(|edge| edge.context.is_none()).collect();
    let all_route_edges: Vec<_> = edges.iter().collect();
    let topologies = [
        (
            "flat64",
            "Physical coordinates are unavailable, so locality and hop metrics are unknown.",
        ),
        (
            "channel_split",
            "Resource index parity approximates two channels; events and flags stay shared.",
        ),
        (
            "stage_hierarchy",
            "Pins/edges, bit resources, logic, byte stages, and control form five ordered groups.",
        ),
        (
            "small_mesh_2x2",
            "Resources are assigned by stable order to four mesh sites; hops are Manhattan distance.",
        ),
        (
            "graph_search_2",
            "Greedy cut minimization under per-resource cluster capacities; local optimum only.",
        ),
        (
            "graph_search_4",
            "Greedy Manhattan-cost minimization on a 2x2 mesh with per-cluster resource capacities; local optimum only.",
        ),
        (
            "graph_search_4_width",
            "Greedy minimization of maximum directed escape-bundle width across all configured links, then routed lane-segments, on a 2x2 mesh; local optimum only.",
        ),
        (
            "graph_search_4_width_activity",
            "Greedy minimization of maximum directed bundle width, then trace-weighted hop-cycles, then lane-segments on a 2x2 mesh; local optimum only.",
        ),
    ];
    topologies
        .into_iter()
        .map(|(topology, assumptions)| {
            let modeled_edges = if topology == "graph_search_4_width"
                || topology == "graph_search_4_width_activity"
            {
                &all_route_edges
            } else {
                &base_edges
            };
            let assignment = placement(resources, topology, modeled_edges, None);
            if topology == "flat64" {
                return TopologyEstimate {
                    topology,
                    local_connections: None,
                    cross_cluster_connections: None,
                    estimated_hops: None,
                    assignments: assignment.into_iter().collect(),
                    assumptions,
                };
            }
            let (local, cross, hops) =
                structural_cost(topology, resources, modeled_edges, &assignment);
            TopologyEstimate {
                topology,
                local_connections: Some(local),
                cross_cluster_connections: Some(cross),
                estimated_hops: Some(hops),
                assignments: assignment.into_iter().collect(),
                assumptions,
            }
        })
        .collect()
}

/// Weight each configured edge by the sampled active cycles for its selected
/// signal line. The profile and edge arrays use the same base-edge ordering.
pub fn estimate_activity(
    resources: &[ResourceAssignment],
    edges: &[SignalConnection],
    active_cycles: &[u64],
) -> Vec<ActivityTopologyEstimate> {
    let base_edges: Vec<_> = edges
        .iter()
        .filter(|edge| edge.context.is_none() && edge.line.is_some())
        .collect();
    let topologies = [
        "flat64",
        "channel_split",
        "stage_hierarchy",
        "small_mesh_2x2",
        "graph_search_2",
        "graph_search_4",
        "graph_search_4_width",
        "graph_search_4_width_activity",
    ];
    topologies
        .into_iter()
        .map(|topology| {
            let assignment = placement(resources, topology, &base_edges, Some(active_cycles));
            if topology == "flat64" {
                return ActivityTopologyEstimate {
                    topology,
                    cross_cluster_active_edge_cycles: None,
                    estimated_active_hop_cycles: None,
                    assignments: assignment.into_iter().collect(),
                };
            }
            let mut cross_cluster_active_edge_cycles = 0u64;
            let mut estimated_active_hop_cycles = 0u64;
            for (edge, &activity) in base_edges.iter().zip(active_cycles) {
                let Some(source) = edge.source.as_deref() else {
                    continue;
                };
                let Some(src) = resources.iter().find(|resource| resource.name == source) else {
                    continue;
                };
                let Some(dst) = resources
                    .iter()
                    .find(|resource| resource.name == edge.destination)
                else {
                    continue;
                };
                let src_group = assignment.get(&src.name).copied().unwrap_or(0);
                let dst_group = assignment.get(&dst.name).copied().unwrap_or(0);
                let hops = hop_distance(topology, src_group, dst_group) as u64;
                if hops != 0 {
                    cross_cluster_active_edge_cycles += activity;
                    estimated_active_hop_cycles += activity * hops;
                }
            }
            ActivityTopologyEstimate {
                topology,
                cross_cluster_active_edge_cycles: Some(cross_cluster_active_edge_cycles),
                estimated_active_hop_cycles: Some(estimated_active_hop_cycles),
                assignments: assignment.into_iter().collect(),
            }
        })
        .collect()
}

/// Sweep abstract escape-link widths for the configured communication graph.
/// A producer signal multicast along a shared route segment counts once there;
/// byte links consume eleven forward lanes and one reverse ready lane;
/// shifter/CRC links consume three forward lanes and one reverse lane.
/// These counts conservatively include all handshake and CRC signals even if
/// a particular configuration does not use every one. Candidate routes and cluster
/// assignments are explicit model assumptions, not inferred coordinates.
pub fn estimate_interconnect_widths(
    resources: &[ResourceAssignment],
    edges: &[SignalConnection],
    capacities: &[usize],
) -> Vec<InterconnectWidthEstimate> {
    // Context banks are alternate configurations. Unioning them here is
    // conservative: the reported width must carry every configured edge even
    // though contexts are never active simultaneously.
    let route_edges: Vec<_> = edges.iter().collect();
    let topologies = [
        "channel_split",
        "stage_hierarchy",
        "small_mesh_2x2",
        "graph_search_2",
        "graph_search_4",
        "graph_search_4_width",
        "graph_search_4_width_activity",
    ];
    let mut estimates = Vec::new();
    for topology in topologies {
        let assignment = placement(resources, topology, &route_edges, None);
        let bundles = routed_bundle_nets(topology, resources, &route_edges, &assignment);
        let total_intercluster_net_segments = bundles
            .values()
            .map(|nets| nets.values().sum::<usize>())
            .sum();
        let maximum_bundle_width = bundles
            .values()
            .map(|nets| nets.values().sum::<usize>())
            .max()
            .unwrap_or(0);
        for &capacity_per_direction in capacities {
            let over_capacity_bundles = bundles
                .iter()
                .filter_map(|(&(from, to), nets)| {
                    let width = nets.values().sum::<usize>();
                    (width > capacity_per_direction)
                        .then(|| (format!("cluster{from}->cluster{to}"), width))
                })
                .collect();
            estimates.push(InterconnectWidthEstimate {
                topology,
                capacity_per_direction,
                total_intercluster_net_segments,
                maximum_bundle_width,
                over_capacity_bundles,
            });
        }
    }
    estimates
}

/// Estimate per-cycle asserted-signal density from a histogram of active
/// dependency sets. `patterns` contains `(asserted_edge_indices, cycle_count)`
/// pairs in the same order as `edges`; it preserves overlap lost by per-edge
/// activity totals. The result does not certify physical link capacity.
pub fn estimate_concurrent_interconnect_demand(
    resources: &[ResourceAssignment],
    edges: &[SignalConnection],
    patterns: &[(Vec<usize>, u64)],
    capacities: &[usize],
) -> Vec<ConcurrentInterconnectEstimate> {
    let base_edges: Vec<_> = edges
        .iter()
        .filter(|edge| edge.context.is_none() && edge.transport == "signal_line")
        .collect();
    let mut edge_cycles = vec![0u64; base_edges.len()];
    for (active, cycles) in patterns {
        for &index in active {
            if let Some(total) = edge_cycles.get_mut(index) {
                *total = total.saturating_add(*cycles);
            }
        }
    }
    let topologies = [
        "channel_split",
        "stage_hierarchy",
        "small_mesh_2x2",
        "graph_search_2",
        "graph_search_4",
        "graph_search_4_width",
        "graph_search_4_width_activity",
    ];
    topologies
        .into_iter()
        .map(|topology| {
            let assignment = placement(resources, topology, &base_edges, Some(&edge_cycles));
            let mut maximum_simultaneous_asserted_nets = 0usize;
            let mut asserted_link_lane_cycles = 0u64;
            let mut cycles_over_capacity = vec![0u64; capacities.len()];
            for (active, cycles) in patterns {
                let mut bundles = BTreeMap::<DirectedBundle, BTreeSet<(String, u8)>>::new();
                for &index in active {
                    let Some(edge) = base_edges.get(index) else {
                        continue;
                    };
                    let (Some(source), Some(line)) = (edge.source.as_deref(), edge.line) else {
                        continue;
                    };
                    let Some(source_resource) = resources.iter().find(|r| r.name == source) else {
                        continue;
                    };
                    let Some(destination_resource) =
                        resources.iter().find(|r| r.name == edge.destination)
                    else {
                        continue;
                    };
                    let source_cluster =
                        assignment.get(&source_resource.name).copied().unwrap_or(0);
                    let destination_cluster = assignment
                        .get(&destination_resource.name)
                        .copied()
                        .unwrap_or(0);
                    let net = (source.to_owned(), line);
                    for bundle in route_bundles(topology, source_cluster, destination_cluster) {
                        bundles.entry(bundle).or_default().insert(net.clone());
                    }
                }
                let mut overloaded = vec![false; capacities.len()];
                for nets in bundles.values() {
                    let width = nets.len();
                    maximum_simultaneous_asserted_nets =
                        maximum_simultaneous_asserted_nets.max(width);
                    asserted_link_lane_cycles = asserted_link_lane_cycles
                        .saturating_add((width as u64).saturating_mul(*cycles));
                    for (index, &capacity) in capacities.iter().enumerate() {
                        overloaded[index] |= width > capacity;
                    }
                }
                for (index, is_overloaded) in overloaded.into_iter().enumerate() {
                    if is_overloaded {
                        cycles_over_capacity[index] =
                            cycles_over_capacity[index].saturating_add(*cycles);
                    }
                }
            }
            ConcurrentInterconnectEstimate {
                topology,
                maximum_simultaneous_asserted_nets,
                asserted_link_lane_cycles,
                cycles_with_asserted_demand_over: capacities
                    .iter()
                    .copied()
                    .zip(cycles_over_capacity)
                    .collect(),
            }
        })
        .collect()
}

fn route_bundles(topology: &str, source: usize, destination: usize) -> Vec<DirectedBundle> {
    if source == destination {
        return Vec::new();
    }
    if topology == "channel_split" || topology == "graph_search_2" {
        // Data regions 0 and 1 connect through the shared controller region
        // 2; inter-channel paths therefore traverse both directed links.
        let mut route = Vec::new();
        let mut current = source;
        if current != 2 && destination != 2 {
            route.push((current, 2));
            current = 2;
        }
        if current != destination {
            route.push((current, destination));
        }
        return route;
    }
    let mut route = Vec::new();
    let mut current = source;
    if topology == "stage_hierarchy" {
        while current != destination {
            let next = if current < destination {
                current + 1
            } else {
                current - 1
            };
            route.push((current, next));
            current = next;
        }
    } else {
        // Deterministic X-then-Y dimension-order routing on the 2x2 mesh.
        while current % 2 != destination % 2 {
            let next = if current % 2 < destination % 2 {
                current + 1
            } else {
                current - 1
            };
            route.push((current, next));
            current = next;
        }
        while current / 2 != destination / 2 {
            let next = if current / 2 < destination / 2 {
                current + 2
            } else {
                current - 2
            };
            route.push((current, next));
            current = next;
        }
    }
    route
}

fn routed_bundle_nets(
    topology: &str,
    resources: &[ResourceAssignment],
    edges: &[&SignalConnection],
    assignment: &BTreeMap<String, usize>,
) -> BTreeMap<DirectedBundle, BTreeMap<String, usize>> {
    let mut bundles = BTreeMap::<DirectedBundle, BTreeMap<String, usize>>::new();
    for edge in edges {
        let Some(source) = edge.source.as_deref() else {
            continue;
        };
        let (net, width, reverse_width) = match edge.transport {
            "signal_line" => {
                let Some(line) = edge.line else { continue };
                (format!("signal:{source}:{line}"), 1, 0)
            }
            "direct_link" => (format!("direct:{source}:{}", edge.destination), 3, 1),
            "byte_link" => (format!("byte:{source}:{}", edge.destination), 11, 1),
            _ => continue,
        };
        let (Some(src), Some(dst)) = (
            resources.iter().find(|resource| resource.name == source),
            resources
                .iter()
                .find(|resource| resource.name == edge.destination),
        ) else {
            continue;
        };
        let source_cluster = assignment.get(&src.name).copied().unwrap_or(0);
        let destination_cluster = assignment.get(&dst.name).copied().unwrap_or(0);
        for bundle in route_bundles(topology, source_cluster, destination_cluster) {
            bundles
                .entry(bundle)
                .or_default()
                .entry(net.clone())
                .or_insert(width);
        }
        if reverse_width != 0 {
            for bundle in route_bundles(topology, destination_cluster, source_cluster) {
                bundles
                    .entry(bundle)
                    .or_default()
                    .entry(format!("{net}:return"))
                    .or_insert(reverse_width);
            }
        }
    }
    bundles
}

/// Weight each configured byte-path edge by observed handshakes on the
/// adjacent byte tile. This uses byte transfers, a separate unit from
/// signal-line active cycles.
pub fn estimate_byte_activity(
    resources: &[ResourceAssignment],
    edges: &[SignalConnection],
    transfers: &[u64],
) -> Vec<ByteActivityTopologyEstimate> {
    let byte_edges: Vec<_> = edges
        .iter()
        .filter(|edge| edge.context.is_none() && edge.transport == "byte_link")
        .collect();
    let topologies = [
        "flat64",
        "channel_split",
        "stage_hierarchy",
        "small_mesh_2x2",
        "graph_search_2",
        "graph_search_4",
        "graph_search_4_width",
        "graph_search_4_width_activity",
    ];
    topologies
        .into_iter()
        .map(|topology| {
            let assignment = placement(resources, topology, &byte_edges, Some(transfers));
            if topology == "flat64" {
                return ByteActivityTopologyEstimate {
                    topology,
                    cross_cluster_transfers: None,
                    estimated_hop_transfers: None,
                    assignments: assignment.into_iter().collect(),
                };
            }
            let mut cross_cluster_transfers = 0u64;
            let mut estimated_hop_transfers = 0u64;
            for (edge, &activity) in byte_edges.iter().zip(transfers) {
                let Some(source) = edge.source.as_deref() else {
                    continue;
                };
                let Some(src) = resources.iter().find(|resource| resource.name == source) else {
                    continue;
                };
                let Some(dst) = resources
                    .iter()
                    .find(|resource| resource.name == edge.destination)
                else {
                    continue;
                };
                let src_group = assignment.get(&src.name).copied().unwrap_or(0);
                let dst_group = assignment.get(&dst.name).copied().unwrap_or(0);
                let hops = hop_distance(topology, src_group, dst_group) as u64;
                if hops != 0 {
                    cross_cluster_transfers += activity;
                    estimated_hop_transfers += activity * hops;
                }
            }
            ByteActivityTopologyEstimate {
                topology,
                cross_cluster_transfers: Some(cross_cluster_transfers),
                estimated_hop_transfers: Some(estimated_hop_transfers),
                assignments: assignment.into_iter().collect(),
            }
        })
        .collect()
}

/// Weight each configured shifter/CRC link by the observed absorb plus emit
/// events on that shifter's direct coder interface.
pub fn estimate_direct_activity(
    resources: &[ResourceAssignment],
    edges: &[SignalConnection],
    events: &[u64],
) -> Vec<DirectActivityTopologyEstimate> {
    let direct_edges: Vec<_> = edges
        .iter()
        .filter(|edge| edge.context.is_none() && edge.transport == "direct_link")
        .collect();
    let topologies = [
        "flat64",
        "channel_split",
        "stage_hierarchy",
        "small_mesh_2x2",
        "graph_search_2",
        "graph_search_4",
        "graph_search_4_width",
        "graph_search_4_width_activity",
    ];
    topologies
        .into_iter()
        .map(|topology| {
            let assignment = placement(resources, topology, &direct_edges, Some(events));
            if topology == "flat64" {
                return DirectActivityTopologyEstimate {
                    topology,
                    cross_cluster_events: None,
                    estimated_hop_events: None,
                    assignments: assignment.into_iter().collect(),
                };
            }
            let mut cross_cluster_events = 0u64;
            let mut estimated_hop_events = 0u64;
            for (edge, &activity) in direct_edges.iter().zip(events) {
                let Some(source) = edge.source.as_deref() else {
                    continue;
                };
                let Some(src) = resources.iter().find(|resource| resource.name == source) else {
                    continue;
                };
                let Some(dst) = resources
                    .iter()
                    .find(|resource| resource.name == edge.destination)
                else {
                    continue;
                };
                let src_group = assignment.get(&src.name).copied().unwrap_or(0);
                let dst_group = assignment.get(&dst.name).copied().unwrap_or(0);
                let hops = hop_distance(topology, src_group, dst_group) as u64;
                if hops != 0 {
                    cross_cluster_events += activity;
                    estimated_hop_events += activity * hops;
                }
            }
            DirectActivityTopologyEstimate {
                topology,
                cross_cluster_events: Some(cross_cluster_events),
                estimated_hop_events: Some(estimated_hop_events),
                assignments: assignment.into_iter().collect(),
            }
        })
        .collect()
}

/// Search for one placement using all available measured communication
/// classes. Signal cycles, byte transfers, and direct-link events are
/// normalized separately to one million weight units per nonempty class.
/// This is an explicit equal-class model assumption, not a physical cost.
pub fn estimate_combined_activity(
    resources: &[ResourceAssignment],
    edges: &[SignalConnection],
    signal_cycles: Option<&[u64]>,
    byte_transfers: Option<&[u64]>,
    direct_events: Option<&[u64]>,
) -> Vec<CombinedActivityTopologyEstimate> {
    estimate_combined_activity_weighted(
        resources,
        edges,
        signal_cycles,
        byte_transfers,
        direct_events,
        [1, 1, 1],
    )
}

/// As [`estimate_combined_activity`], with explicit relative weights for
/// signal-line, byte-path, and direct-link activity classes.
pub fn estimate_combined_activity_weighted(
    resources: &[ResourceAssignment],
    edges: &[SignalConnection],
    signal_cycles: Option<&[u64]>,
    byte_transfers: Option<&[u64]>,
    direct_events: Option<&[u64]>,
    class_weights: [u64; 3],
) -> Vec<CombinedActivityTopologyEstimate> {
    const SCALE: u64 = 1_000_000;

    let mut weighted_edges = Vec::<&SignalConnection>::new();
    let mut weights = Vec::<u64>::new();
    let mut categories: Vec<TrafficCategory<'_>> = Vec::new();
    for (index, (transport, activity)) in [
        ("signal_line", signal_cycles),
        ("byte_link", byte_transfers),
        ("direct_link", direct_events),
    ]
    .into_iter()
    .enumerate()
    {
        let class_weight = class_weights[index];
        if class_weight == 0 {
            continue;
        }
        let Some(activity) = activity else { continue };
        let selected: Vec<_> = edges
            .iter()
            .filter(|edge| edge.context.is_none() && edge.transport == transport)
            .collect();
        if selected.len() != activity.len() {
            continue;
        }
        let total: u64 = activity.iter().sum();
        if total == 0 {
            continue;
        }
        let normalized: Vec<_> = selected
            .into_iter()
            .zip(activity.iter().copied())
            .map(|(edge, count)| {
                (
                    edge,
                    ((count as u128 * SCALE as u128 * class_weight as u128) / total as u128) as u64,
                    count,
                )
            })
            .collect();
        weighted_edges.extend(normalized.iter().map(|(edge, _, _)| *edge));
        weights.extend(normalized.iter().map(|(_, weight, _)| *weight));
        categories.push((transport, normalized));
    }

    let topologies = [
        "flat64",
        "channel_split",
        "stage_hierarchy",
        "small_mesh_2x2",
        "graph_search_2",
        "graph_search_4",
        "graph_search_4_width",
        "graph_search_4_width_activity",
    ];
    topologies
        .into_iter()
        .map(|topology| {
            let assignment = placement(resources, topology, &weighted_edges, Some(&weights));
            if topology == "flat64" {
                return CombinedActivityTopologyEstimate {
                    topology,
                    normalized_cross_cost: None,
                    signal_cross_cycles: None,
                    byte_cross_transfers: None,
                    direct_cross_events: None,
                    assignments: assignment.into_iter().collect(),
                };
            }

            let mut normalized_cross_cost = 0;
            let mut raw_costs = BTreeMap::new();
            for (transport, category) in &categories {
                let mut raw_cross = 0u64;
                for (edge, weight, count) in category {
                    let Some(source) = edge.source.as_deref() else {
                        continue;
                    };
                    let Some(source_resource) = resources.iter().find(|r| r.name == source) else {
                        continue;
                    };
                    let Some(destination_resource) =
                        resources.iter().find(|r| r.name == edge.destination)
                    else {
                        continue;
                    };
                    let source_cluster =
                        assignment.get(&source_resource.name).copied().unwrap_or(0);
                    let destination_cluster = assignment
                        .get(&destination_resource.name)
                        .copied()
                        .unwrap_or(0);
                    let hops = hop_distance(topology, source_cluster, destination_cluster) as u64;
                    if hops != 0 {
                        raw_cross += count;
                        normalized_cross_cost += weight * hops;
                    }
                }
                raw_costs.insert(*transport, raw_cross);
            }
            CombinedActivityTopologyEstimate {
                topology,
                normalized_cross_cost: Some(normalized_cross_cost),
                signal_cross_cycles: categories
                    .iter()
                    .any(|(kind, _)| *kind == "signal_line")
                    .then(|| raw_costs.get("signal_line").copied().unwrap_or(0)),
                byte_cross_transfers: categories
                    .iter()
                    .any(|(kind, _)| *kind == "byte_link")
                    .then(|| raw_costs.get("byte_link").copied().unwrap_or(0)),
                direct_cross_events: categories
                    .iter()
                    .any(|(kind, _)| *kind == "direct_link")
                    .then(|| raw_costs.get("direct_link").copied().unwrap_or(0)),
                assignments: assignment.into_iter().collect(),
            }
        })
        .collect()
}

fn placement(
    resources: &[ResourceAssignment],
    topology: &str,
    edges: &[&SignalConnection],
    weights: Option<&[u64]>,
) -> BTreeMap<String, usize> {
    if topology == "graph_search_2" {
        return search_two_cluster(resources, edges, weights);
    }
    if topology == "graph_search_4" {
        return search_four_cluster(resources, edges, weights);
    }
    if topology == "graph_search_4_width" {
        return search_four_cluster_width(resources, edges);
    }
    if topology == "graph_search_4_width_activity" {
        return search_four_cluster_width_activity(resources, edges, weights);
    }
    resources
        .iter()
        .map(|resource| {
            let cluster = match topology {
                "flat64" => 0,
                "channel_split" => channel_group(resource),
                "stage_hierarchy" => stage_group(resource),
                "small_mesh_2x2" => mesh_site(resource),
                _ => unreachable!("unknown topology"),
            };
            (resource.name.clone(), cluster)
        })
        .collect()
}

fn structural_cost(
    topology: &str,
    resources: &[ResourceAssignment],
    edges: &[&SignalConnection],
    assignment: &BTreeMap<String, usize>,
) -> (usize, usize, usize) {
    let mut local = 0;
    let mut cross = 0;
    let mut hops = 0;
    for edge in edges {
        let Some(source) = edge.source.as_deref() else {
            continue;
        };
        if !resources.iter().any(|resource| resource.name == source)
            || !resources
                .iter()
                .any(|resource| resource.name == edge.destination)
        {
            continue;
        }
        let src = assignment.get(source).copied().unwrap_or(0);
        let dst = assignment.get(&edge.destination).copied().unwrap_or(0);
        let distance = hop_distance(topology, src, dst);
        if distance == 0 {
            local += 1;
        } else {
            cross += 1;
            hops += distance;
        }
    }
    (local, cross, hops)
}

fn hop_distance(topology: &str, source: usize, destination: usize) -> usize {
    match topology {
        "channel_split" | "graph_search_2" => route_bundles(topology, source, destination).len(),
        "small_mesh_2x2"
        | "graph_search_4"
        | "graph_search_4_width"
        | "graph_search_4_width_activity" => {
            (source / 2).abs_diff(destination / 2) + (source % 2).abs_diff(destination % 2)
        }
        "stage_hierarchy" => source.abs_diff(destination),
        _ => usize::from(source != destination),
    }
}

fn search_two_cluster(
    resources: &[ResourceAssignment],
    edges: &[&SignalConnection],
    weights: Option<&[u64]>,
) -> BTreeMap<String, usize> {
    let mut assignment: BTreeMap<_, _> = resources
        .iter()
        .map(|resource| (resource.name.clone(), channel_group(resource)))
        .collect();
    loop {
        let mut best = assignment.clone();
        let mut best_cost = cut_cost(resources, edges, weights, &assignment);
        let mut candidates = Vec::<BTreeMap<String, usize>>::new();

        for resource in resources {
            let Some(capacity) = per_cluster_capacity(resource.kind) else {
                continue;
            };
            let Some(&cluster) = assignment.get(&resource.name) else {
                continue;
            };
            if cluster > 1 {
                continue;
            }
            let target = 1 - cluster;
            let occupancy = resources
                .iter()
                .filter(|other| {
                    other.kind == resource.kind
                        && assignment.get(&other.name).copied() == Some(target)
                })
                .count();
            if occupancy < capacity {
                let mut candidate = assignment.clone();
                candidate.insert(resource.name.clone(), target);
                candidates.push(candidate);
            }
        }

        for (left_index, left) in resources.iter().enumerate() {
            let Some(&left_cluster) = assignment.get(&left.name) else {
                continue;
            };
            if left_cluster > 1 || per_cluster_capacity(left.kind).is_none() {
                continue;
            }
            for right in &resources[left_index + 1..] {
                let Some(&right_cluster) = assignment.get(&right.name) else {
                    continue;
                };
                if left.kind == right.kind && right_cluster == 1 - left_cluster {
                    let mut candidate = assignment.clone();
                    candidate.insert(left.name.clone(), right_cluster);
                    candidate.insert(right.name.clone(), left_cluster);
                    candidates.push(candidate);
                }
            }
        }

        for candidate in candidates {
            let cost = cut_cost(resources, edges, weights, &candidate);
            if cost < best_cost {
                best = candidate;
                best_cost = cost;
            }
        }
        if best == assignment {
            return assignment;
        }
        assignment = best;
    }
}

fn search_four_cluster(
    resources: &[ResourceAssignment],
    edges: &[&SignalConnection],
    weights: Option<&[u64]>,
) -> BTreeMap<String, usize> {
    let mesh_seed: BTreeMap<_, _> = resources
        .iter()
        .map(|resource| (resource.name.clone(), mesh_site(resource)))
        .collect();
    let channel_seed = four_cluster_channel_seed(resources);
    let mesh_result = improve_four_cluster(resources, edges, weights, mesh_seed);
    let channel_result = improve_four_cluster(resources, edges, weights, channel_seed);
    if mesh_cost(resources, edges, weights, &channel_result)
        < mesh_cost(resources, edges, weights, &mesh_result)
    {
        channel_result
    } else {
        mesh_result
    }
}

fn four_cluster_channel_seed(resources: &[ResourceAssignment]) -> BTreeMap<String, usize> {
    let mut assignment: BTreeMap<_, _> = resources
        .iter()
        .map(|resource| (resource.name.clone(), mesh_site(resource)))
        .collect();
    let mut ordered: Vec<_> = resources.iter().collect();
    ordered.sort_by_key(|resource| (resource.kind, channel_group(resource), resource.index));
    for resource in ordered {
        let Some(capacity) = four_cluster_capacity(resource.kind) else {
            continue;
        };
        let preferred = match channel_group(resource) {
            0 => [0, 2],
            1 => [1, 3],
            _ => [2, 3],
        };
        let target = preferred.into_iter().chain(0..4).find(|&cluster| {
            resources
                .iter()
                .filter(|other| {
                    other.kind == resource.kind
                        && assignment.get(&other.name).copied() == Some(cluster)
                })
                .count()
                < capacity
        });
        if let Some(target) = target {
            assignment.insert(resource.name.clone(), target);
        }
    }
    assignment
}

fn improve_four_cluster(
    resources: &[ResourceAssignment],
    edges: &[&SignalConnection],
    weights: Option<&[u64]>,
    mut assignment: BTreeMap<String, usize>,
) -> BTreeMap<String, usize> {
    loop {
        let mut best = assignment.clone();
        let mut best_cost = mesh_cost(resources, edges, weights, &assignment);
        let mut candidates = Vec::<BTreeMap<String, usize>>::new();

        for resource in resources {
            let Some(capacity) = four_cluster_capacity(resource.kind) else {
                continue;
            };
            let Some(&source) = assignment.get(&resource.name) else {
                continue;
            };
            for target in 0..4 {
                if target == source {
                    continue;
                }
                let occupancy = resources
                    .iter()
                    .filter(|other| {
                        other.kind == resource.kind
                            && assignment.get(&other.name).copied() == Some(target)
                    })
                    .count();
                if occupancy < capacity {
                    let mut candidate = assignment.clone();
                    candidate.insert(resource.name.clone(), target);
                    candidates.push(candidate);
                }
            }
        }

        for (left_index, left) in resources.iter().enumerate() {
            let Some(&left_cluster) = assignment.get(&left.name) else {
                continue;
            };
            if four_cluster_capacity(left.kind).is_none() {
                continue;
            }
            for right in &resources[left_index + 1..] {
                let Some(&right_cluster) = assignment.get(&right.name) else {
                    continue;
                };
                if left.kind == right.kind && right_cluster != left_cluster {
                    let mut candidate = assignment.clone();
                    candidate.insert(left.name.clone(), right_cluster);
                    candidate.insert(right.name.clone(), left_cluster);
                    candidates.push(candidate);
                }
            }
        }

        for candidate in candidates {
            let cost = mesh_cost(resources, edges, weights, &candidate);
            if cost < best_cost {
                best = candidate;
                best_cost = cost;
            }
        }
        if best == assignment {
            return assignment;
        }
        assignment = best;
    }
}

/// Search a 2x2 placement for a narrow escape fabric. Unlike the hop search,
/// this prioritizes the worst directed bundle width, then total routed net
/// segments. It intentionally ignores activity weights: it is a static
/// connectivity mapper.
fn search_four_cluster_width(
    resources: &[ResourceAssignment],
    edges: &[&SignalConnection],
) -> BTreeMap<String, usize> {
    let mesh_seed: BTreeMap<_, _> = resources
        .iter()
        .map(|resource| (resource.name.clone(), mesh_site(resource)))
        .collect();
    let channel_seed = four_cluster_channel_seed(resources);
    let mesh_result = improve_four_cluster_width(resources, edges, mesh_seed);
    let channel_result = improve_four_cluster_width(resources, edges, channel_seed);
    if width_cost(resources, edges, &channel_result) < width_cost(resources, edges, &mesh_result) {
        channel_result
    } else {
        mesh_result
    }
}

fn improve_four_cluster_width(
    resources: &[ResourceAssignment],
    edges: &[&SignalConnection],
    mut assignment: BTreeMap<String, usize>,
) -> BTreeMap<String, usize> {
    loop {
        let mut best = assignment.clone();
        let mut best_cost = width_cost(resources, edges, &assignment);
        let mut candidates = Vec::<BTreeMap<String, usize>>::new();

        for resource in resources {
            let Some(capacity) = four_cluster_capacity(resource.kind) else {
                continue;
            };
            let Some(&source) = assignment.get(&resource.name) else {
                continue;
            };
            for target in 0..4 {
                if target == source {
                    continue;
                }
                let occupancy = resources
                    .iter()
                    .filter(|other| {
                        other.kind == resource.kind
                            && assignment.get(&other.name).copied() == Some(target)
                    })
                    .count();
                if occupancy < capacity {
                    let mut candidate = assignment.clone();
                    candidate.insert(resource.name.clone(), target);
                    candidates.push(candidate);
                }
            }
        }

        for (left_index, left) in resources.iter().enumerate() {
            let Some(&left_cluster) = assignment.get(&left.name) else {
                continue;
            };
            if four_cluster_capacity(left.kind).is_none() {
                continue;
            }
            for right in &resources[left_index + 1..] {
                let Some(&right_cluster) = assignment.get(&right.name) else {
                    continue;
                };
                if left.kind == right.kind && right_cluster != left_cluster {
                    let mut candidate = assignment.clone();
                    candidate.insert(left.name.clone(), right_cluster);
                    candidate.insert(right.name.clone(), left_cluster);
                    candidates.push(candidate);
                }
            }
        }

        for candidate in candidates {
            let cost = width_cost(resources, edges, &candidate);
            if cost < best_cost {
                best = candidate;
                best_cost = cost;
            }
        }
        if best == assignment {
            return assignment;
        }
        assignment = best;
    }
}

fn search_four_cluster_width_activity(
    resources: &[ResourceAssignment],
    edges: &[&SignalConnection],
    weights: Option<&[u64]>,
) -> BTreeMap<String, usize> {
    let seeds = [
        resources
            .iter()
            .map(|r| (r.name.clone(), mesh_site(r)))
            .collect(),
        four_cluster_channel_seed(resources),
    ];
    seeds
        .into_iter()
        .map(|seed| improve_four_cluster_width_activity(resources, edges, weights, seed))
        .min_by_key(|a| width_activity_cost(resources, edges, weights, a))
        .unwrap_or_default()
}

fn improve_four_cluster_width_activity(
    resources: &[ResourceAssignment],
    edges: &[&SignalConnection],
    weights: Option<&[u64]>,
    mut assignment: BTreeMap<String, usize>,
) -> BTreeMap<String, usize> {
    loop {
        let mut best = assignment.clone();
        let mut best_cost = width_activity_cost(resources, edges, weights, &assignment);
        let mut candidates = Vec::new();
        for resource in resources {
            let Some(capacity) = four_cluster_capacity(resource.kind) else {
                continue;
            };
            let Some(&source) = assignment.get(&resource.name) else {
                continue;
            };
            for target in 0..4 {
                if target == source {
                    continue;
                }
                let occupancy = resources
                    .iter()
                    .filter(|other| {
                        other.kind == resource.kind && assignment.get(&other.name) == Some(&target)
                    })
                    .count();
                if occupancy < capacity {
                    let mut candidate = assignment.clone();
                    candidate.insert(resource.name.clone(), target);
                    candidates.push(candidate);
                }
            }
        }
        for (i, left) in resources.iter().enumerate() {
            if four_cluster_capacity(left.kind).is_none() {
                continue;
            }
            let Some(&lc) = assignment.get(&left.name) else {
                continue;
            };
            for right in &resources[i + 1..] {
                if left.kind != right.kind {
                    continue;
                }
                let Some(&rc) = assignment.get(&right.name) else {
                    continue;
                };
                if lc != rc {
                    let mut candidate = assignment.clone();
                    candidate.insert(left.name.clone(), rc);
                    candidate.insert(right.name.clone(), lc);
                    candidates.push(candidate);
                }
            }
        }
        for candidate in candidates {
            let cost = width_activity_cost(resources, edges, weights, &candidate);
            if cost < best_cost {
                best = candidate;
                best_cost = cost;
            }
        }
        if best == assignment {
            return assignment;
        }
        assignment = best;
    }
}

fn width_activity_cost(
    resources: &[ResourceAssignment],
    edges: &[&SignalConnection],
    weights: Option<&[u64]>,
    assignment: &BTreeMap<String, usize>,
) -> (usize, u64, usize) {
    let (max_width, segments) = width_cost(resources, edges, assignment);
    let weighted_hops = edges
        .iter()
        .enumerate()
        .filter_map(|(i, e)| {
            let s = assignment.get(e.source.as_deref()?)?;
            let d = assignment.get(&e.destination)?;
            let known = resources
                .iter()
                .any(|r| r.name == e.source.as_deref().unwrap_or_default())
                && resources.iter().any(|r| r.name == e.destination);
            known.then(|| {
                weights.and_then(|w| w.get(i)).copied().unwrap_or(1)
                    * hop_distance("graph_search_4", *s, *d) as u64
            })
        })
        .sum();
    (max_width, weighted_hops, segments)
}

fn width_cost(
    resources: &[ResourceAssignment],
    edges: &[&SignalConnection],
    assignment: &BTreeMap<String, usize>,
) -> (usize, usize) {
    let bundles = routed_bundle_nets("graph_search_4_width", resources, edges, assignment);
    (
        bundles
            .values()
            .map(|nets| nets.values().sum::<usize>())
            .max()
            .unwrap_or(0),
        bundles
            .values()
            .map(|nets| nets.values().sum::<usize>())
            .sum(),
    )
}

fn mesh_cost(
    resources: &[ResourceAssignment],
    edges: &[&SignalConnection],
    weights: Option<&[u64]>,
    assignment: &BTreeMap<String, usize>,
) -> u64 {
    edges
        .iter()
        .enumerate()
        .filter_map(|(index, edge)| {
            let source = edge.source.as_deref()?;
            let source_cluster = assignment.get(source)?;
            let destination_cluster = assignment.get(&edge.destination)?;
            let known = resources.iter().any(|resource| resource.name == source)
                && resources
                    .iter()
                    .any(|resource| resource.name == edge.destination);
            known.then(|| {
                weights
                    .and_then(|weights| weights.get(index))
                    .copied()
                    .unwrap_or(1)
                    * hop_distance("graph_search_4", *source_cluster, *destination_cluster) as u64
            })
        })
        .sum()
}

fn cut_cost(
    resources: &[ResourceAssignment],
    edges: &[&SignalConnection],
    weights: Option<&[u64]>,
    assignment: &BTreeMap<String, usize>,
) -> u64 {
    edges
        .iter()
        .enumerate()
        .filter_map(|(index, edge)| {
            let source = edge.source.as_deref()?;
            let source_cluster = assignment.get(source)?;
            let destination_cluster = assignment.get(&edge.destination)?;
            let known = resources.iter().any(|resource| resource.name == source)
                && resources
                    .iter()
                    .any(|resource| resource.name == edge.destination);
            (known && source_cluster != destination_cluster).then(|| {
                weights
                    .and_then(|weights| weights.get(index))
                    .copied()
                    .unwrap_or(1)
            })
        })
        .sum()
}

/// Maximum resources of this class in one of the two abstract clusters.
/// Pins, shifters, edge detectors, byte stages, and shared control stay fixed.
fn per_cluster_capacity(kind: &str) -> Option<usize> {
    match kind {
        "timer" => Some(2),
        "counter" | "CRC" | "protocol actor" => Some(1),
        "logic cell" => Some(4),
        _ => None,
    }
}

fn four_cluster_capacity(kind: &str) -> Option<usize> {
    match kind {
        "timer" | "counter" | "CRC" | "protocol actor" | "shifter" | "PX8 protocol controller" => {
            Some(1)
        }
        "logic cell" => Some(2),
        "byte pass" | "byte escape" | "byte unescape" | "byte checksum" | "byte tag"
        | "byte filter" | "byte stage" => Some(2),
        _ => None,
    }
}

fn channel_group(resource: &ResourceAssignment) -> usize {
    match resource.kind {
        "event" | "flag" | "PX8 protocol controller" => 2,
        "byte pass" | "byte escape" | "byte unescape" | "byte checksum" | "byte tag"
        | "byte filter" | "byte stage" => resource.index / 4,
        _ => resource.index % 2,
    }
}

fn stage_group(resource: &ResourceAssignment) -> usize {
    match resource.kind {
        "pin cell" | "edge cell" => 0,
        "timer" | "shifter" | "counter" | "CRC" => 1,
        "logic cell" => 2,
        "byte pass" | "byte escape" | "byte unescape" | "byte checksum" | "byte tag"
        | "byte filter" | "byte stage" => 3,
        _ => 4,
    }
}

fn mesh_site(resource: &ResourceAssignment) -> usize {
    let kind_slot = match resource.kind {
        "pin cell" | "edge cell" => 0,
        "timer" | "counter" => 1,
        "shifter" | "CRC" => 2,
        "logic cell" => 3,
        "byte pass" | "byte escape" | "byte unescape" | "byte checksum" | "byte tag"
        | "byte filter" | "byte stage" => 4,
        _ => 5,
    };
    (kind_slot + resource.index) % 4
}

#[cfg(test)]
mod tests {
    use super::*;

    fn resource(
        name: &str,
        kind: &'static str,
        index: usize,
        outputs: &[(u8,)],
    ) -> ResourceAssignment {
        ResourceAssignment {
            name: name.into(),
            kind,
            index,
            config: None,
            outputs: outputs.iter().map(|(line,)| ("out", *line)).collect(),
        }
    }

    #[test]
    fn channel_split_routes_inter_channel_edges_through_shared_control_region() {
        assert_eq!(route_bundles("channel_split", 0, 1), [(0, 2), (2, 1)]);
        assert_eq!(route_bundles("channel_split", 1, 0), [(1, 2), (2, 0)]);
        assert_eq!(route_bundles("channel_split", 0, 2), [(0, 2)]);
        assert_eq!(route_bundles("graph_search_2", 0, 1), [(0, 2), (2, 1)]);
        assert_eq!(hop_distance("channel_split", 0, 1), 2);
        assert_eq!(hop_distance("graph_search_2", 0, 1), 2);
    }

    #[test]
    fn mesh_routes_use_directional_bundles_and_shortest_xy_paths() {
        assert_eq!(route_bundles("small_mesh_2x2", 0, 3), vec![(0, 1), (1, 3)]);
        assert_eq!(route_bundles("small_mesh_2x2", 3, 0), vec![(3, 2), (2, 0)]);
        assert_eq!(route_bundles("small_mesh_2x2", 2, 2), Vec::new());
    }

    #[test]
    fn width_sweep_deduplicates_shared_multicast_and_flags_narrow_links() {
        let resources = vec![
            resource("source_a", "pin cell", 0, &[(3,)]),
            resource("source_b", "pin cell", 2, &[(4,)]),
            resource("sink_a", "pin cell", 1, &[]),
            resource("sink_b", "pin cell", 3, &[]),
            resource("controller", "PX8 protocol controller", 0, &[]),
        ];
        let edge = |source: &str, line, destination: &str| SignalConnection {
            source: Some(source.into()),
            line: Some(line),
            transport: "signal_line",
            source_index: None,
            destination: destination.into(),
            input: "test.input".into(),
            context: None,
        };
        let mut contextual_edge = edge("controller", 4, "sink_a");
        contextual_edge.context = Some(0);
        let estimates = estimate_interconnect_widths(
            &resources,
            &[
                edge("source_a", 3, "sink_a"),
                edge("source_a", 3, "sink_b"),
                edge("source_a", 3, "controller"),
                edge("source_b", 4, "sink_a"),
                contextual_edge,
            ],
            &[1, 2, 3],
        );
        let one_lane = estimates
            .iter()
            .find(|estimate| {
                estimate.topology == "channel_split" && estimate.capacity_per_direction == 1
            })
            .unwrap();
        assert_eq!(one_lane.maximum_bundle_width, 3);
        assert_eq!(one_lane.total_intercluster_net_segments, 5);
        assert_eq!(one_lane.over_capacity_bundles.len(), 2);
        let two_lanes = estimates
            .iter()
            .find(|estimate| {
                estimate.topology == "channel_split" && estimate.capacity_per_direction == 2
            })
            .unwrap();
        assert_eq!(two_lanes.over_capacity_bundles.len(), 1);
        let three_lanes = estimates
            .iter()
            .find(|estimate| {
                estimate.topology == "channel_split" && estimate.capacity_per_direction == 3
            })
            .unwrap();
        assert!(three_lanes.over_capacity_bundles.is_empty());
    }

    #[test]
    fn width_sweep_counts_byte_and_direct_paths_in_scalar_lanes() {
        let resources = vec![
            resource("source", "pin cell", 0, &[]),
            resource("sink", "pin cell", 1, &[]),
        ];
        let edge = |transport, source: &str, destination: &str| SignalConnection {
            source: Some(source.into()),
            line: None,
            transport,
            source_index: None,
            destination: destination.into(),
            input: "test.data".into(),
            context: None,
        };
        let estimates = estimate_interconnect_widths(
            &resources,
            &[
                edge("byte_link", "source", "sink"),
                edge("direct_link", "source", "sink"),
            ],
            &[13, 14],
        );
        let eight_lanes = estimates
            .iter()
            .find(|estimate| {
                estimate.topology == "channel_split" && estimate.capacity_per_direction == 13
            })
            .unwrap();
        assert_eq!(eight_lanes.maximum_bundle_width, 14);
        assert_eq!(eight_lanes.total_intercluster_net_segments, 32);
        assert_eq!(eight_lanes.over_capacity_bundles.len(), 2);
        let nine_lanes = estimates
            .iter()
            .find(|estimate| {
                estimate.topology == "channel_split" && estimate.capacity_per_direction == 14
            })
            .unwrap();
        assert!(nine_lanes.over_capacity_bundles.is_empty());
    }

    #[test]
    fn estimates_distinguish_flat_and_stage_local_edges() {
        let resources = vec![
            resource("rx", "pin cell", 0, &[(32,)]),
            resource("sample", "logic cell", 0, &[(56,)]),
            resource("tx", "pin cell", 1, &[(33,)]),
        ];
        let edges = connect_producers(
            &resources,
            vec![
                SignalConnection {
                    source: None,
                    line: Some(32),
                    transport: "signal_line",
                    source_index: None,
                    destination: "sample".into(),
                    input: "logic.in0".into(),
                    context: None,
                },
                SignalConnection {
                    source: None,
                    line: Some(56),
                    transport: "signal_line",
                    source_index: None,
                    destination: "tx".into(),
                    input: "pin.drive".into(),
                    context: None,
                },
            ],
        );
        let estimates = estimate_topologies(&resources, &edges);
        assert_eq!(estimates[0].local_connections, None);
        assert_eq!(estimates[0].cross_cluster_connections, None);
        assert!(estimates[2].cross_cluster_connections.unwrap() > 0);
        assert!(estimates[2].estimated_hops.unwrap() > 0);
    }

    #[test]
    fn decodes_level_encoded_pin_and_timer_selectors() {
        let pin = decode_selectors("pin", "tx", 1, &[(0x19, 30 | (1 << 6))], None);
        assert_eq!(pin.len(), 1);
        assert_eq!(pin[0].line, Some(62));
        assert_eq!(pin[0].input, "pin.drive");

        let timer = decode_selectors(
            "timer",
            "bit",
            0,
            &[(2, 4 | (2 << 6)), (3, (1 << 12) | (24 << 7))],
            None,
        );
        assert_eq!(
            timer.iter().map(|edge| edge.line).collect::<Vec<_>>(),
            [Some(4), Some(2), Some(56)]
        );
        assert_eq!(timer[0].input, "timer.start");
        assert_eq!(timer[2].input, "timer.hold");
    }

    #[test]
    fn decodes_both_protocol_actor_input_selectors() {
        let actor = decode_selectors("protocol", "control", 0, &[(0xa0, 32 | (33 << 6))], None);
        assert_eq!(actor.len(), 2);
        assert_eq!(actor[0].line, Some(32));
        assert_eq!(actor[0].input, "protocol.input");
        assert_eq!(actor[1].line, Some(33));
        assert_eq!(actor[1].input, "protocol.input2");
    }

    #[test]
    fn activity_weights_cross_cluster_edges() {
        let resources = vec![
            resource("rx", "pin cell", 0, &[(32,)]),
            resource("sample", "logic cell", 0, &[(56,)]),
            resource("tx", "pin cell", 1, &[(33,)]),
        ];
        let edges = connect_producers(
            &resources,
            vec![
                SignalConnection {
                    source: None,
                    line: Some(32),
                    transport: "signal_line",
                    source_index: None,
                    destination: "sample".into(),
                    input: "logic.in0".into(),
                    context: None,
                },
                SignalConnection {
                    source: None,
                    line: Some(56),
                    transport: "signal_line",
                    source_index: None,
                    destination: "tx".into(),
                    input: "pin.drive".into(),
                    context: None,
                },
            ],
        );
        let estimates = estimate_activity(&resources, &edges, &[9, 1]);
        assert_eq!(estimates[1].cross_cluster_active_edge_cycles, Some(1));
        assert_eq!(estimates[2].cross_cluster_active_edge_cycles, Some(10));
        assert_eq!(estimates[2].estimated_active_hop_cycles, Some(20));
    }

    #[test]
    fn two_cluster_search_reduces_cut_without_exceeding_resource_capacity() {
        let resources = vec![
            resource("logic0", "logic cell", 0, &[(56,)]),
            resource("logic1", "logic cell", 1, &[(57,)]),
            resource("timer0", "timer", 0, &[(40,)]),
            resource("timer1", "timer", 1, &[(41,)]),
        ];
        let edges = connect_producers(
            &resources,
            vec![
                SignalConnection {
                    source: None,
                    line: Some(56),
                    transport: "signal_line",
                    source_index: None,
                    destination: "timer1".into(),
                    input: "timer.start".into(),
                    context: None,
                },
                SignalConnection {
                    source: None,
                    line: Some(57),
                    transport: "signal_line",
                    source_index: None,
                    destination: "timer0".into(),
                    input: "timer.start".into(),
                    context: None,
                },
            ],
        );
        let estimates = estimate_topologies(&resources, &edges);
        let channel = estimates
            .iter()
            .find(|e| e.topology == "channel_split")
            .unwrap();
        let searched = estimates
            .iter()
            .find(|e| e.topology == "graph_search_2")
            .unwrap();
        assert_eq!(channel.cross_cluster_connections, Some(2));
        assert_eq!(searched.cross_cluster_connections, Some(0));
        for kind in ["logic cell", "timer"] {
            for cluster in 0..2 {
                let count = searched
                    .assignments
                    .iter()
                    .filter(|(name, assigned)| {
                        *assigned == cluster
                            && resources
                                .iter()
                                .any(|resource| resource.name == *name && resource.kind == kind)
                    })
                    .count();
                assert!(count <= per_cluster_capacity(kind).unwrap());
            }
        }
    }

    #[test]
    fn four_cluster_search_uses_local_routes_and_respects_tile_capacity() {
        let mut resources = Vec::new();
        let mut edges = Vec::new();
        for index in 0..4 {
            resources.push(resource(&format!("logic{index}"), "logic cell", index, &[]));
            resources.push(resource(&format!("timer{index}"), "timer", index, &[]));
            edges.push(SignalConnection {
                source: Some(format!("logic{index}")),
                line: Some(32 + index as u8),
                transport: "signal_line",
                source_index: None,
                destination: format!("timer{index}"),
                input: "timer.start".into(),
                context: None,
            });
        }

        let estimates = estimate_topologies(&resources, &edges);
        let searched = estimates
            .iter()
            .find(|estimate| estimate.topology == "graph_search_4")
            .unwrap();
        assert_eq!(searched.estimated_hops, Some(0));
        for kind in ["logic cell", "timer", "counter", "CRC", "protocol actor"] {
            for cluster in 0..4 {
                let count = searched
                    .assignments
                    .iter()
                    .filter(|(name, assigned)| {
                        *assigned == cluster
                            && resources
                                .iter()
                                .any(|resource| resource.name == *name && resource.kind == kind)
                    })
                    .count();
                if let Some(capacity) = four_cluster_capacity(kind) {
                    assert!(count <= capacity);
                } else {
                    assert_eq!(count, 0);
                }
            }
        }
    }

    #[test]
    fn activity_width_search_preserves_width_objective_and_respects_capacity() {
        let resources = vec![
            resource("logic0", "logic cell", 0, &[]),
            resource("logic1", "logic cell", 1, &[]),
            resource("timer0", "timer", 0, &[]),
            resource("timer1", "timer", 1, &[]),
        ];
        let edges = [
            SignalConnection {
                source: Some("logic0".into()),
                line: Some(32),
                transport: "signal_line",
                source_index: None,
                destination: "timer0".into(),
                input: "start".into(),
                context: None,
            },
            SignalConnection {
                source: Some("logic1".into()),
                line: Some(33),
                transport: "signal_line",
                source_index: None,
                destination: "timer1".into(),
                input: "start".into(),
                context: None,
            },
            SignalConnection {
                source: Some("logic0".into()),
                line: Some(32),
                transport: "signal_line",
                source_index: None,
                destination: "timer1".into(),
                input: "start".into(),
                context: None,
            },
        ];
        let edge_refs: Vec<_> = edges.iter().collect();
        let baseline: BTreeMap<_, _> = resources
            .iter()
            .map(|r| (r.name.clone(), mesh_site(r)))
            .collect();
        let searched =
            search_four_cluster_width_activity(&resources, &edge_refs, Some(&[100, 1, 5]));
        let base_cost = width_cost(&resources, &edge_refs, &baseline).0;
        let searched_cost = width_cost(&resources, &edge_refs, &searched).0;
        assert!(searched_cost <= base_cost);
        for kind in ["logic cell", "timer"] {
            for cluster in 0..4 {
                let count = resources
                    .iter()
                    .filter(|r| r.kind == kind && searched.get(&r.name) == Some(&cluster))
                    .count();
                assert!(count <= four_cluster_capacity(kind).unwrap());
            }
        }
    }

    #[test]
    fn concurrent_link_demand_counts_cycles_and_deduplicates_multicast() {
        let resources = vec![
            resource("source_a", "pin cell", 0, &[]),
            resource("sink_a", "pin cell", 1, &[]),
            resource("source_b", "pin cell", 2, &[]),
            resource("sink_b", "pin cell", 3, &[]),
        ];
        let edge = |source: &str, line, destination: &str| SignalConnection {
            source: Some(source.into()),
            line: Some(line),
            transport: "signal_line",
            source_index: None,
            destination: destination.into(),
            input: "test.input".into(),
            context: None,
        };
        let estimates = estimate_concurrent_interconnect_demand(
            &resources,
            &[
                edge("source_a", 32, "sink_a"),
                edge("source_a", 32, "sink_b"),
                edge("source_b", 33, "sink_b"),
            ],
            &[(vec![0, 1, 2], 5), (vec![0], 3)],
            &[1, 2],
        );
        let channel_split = estimates
            .iter()
            .find(|estimate| estimate.topology == "channel_split")
            .unwrap();
        assert_eq!(channel_split.maximum_simultaneous_asserted_nets, 2);
        assert_eq!(channel_split.asserted_link_lane_cycles, 26);
        assert_eq!(
            channel_split.cycles_with_asserted_demand_over,
            vec![(1, 5), (2, 0)]
        );
    }
}
