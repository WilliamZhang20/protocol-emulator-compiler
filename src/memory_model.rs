//! Deterministic, dependency-aware timing experiments for abstract memory banks.
//!
//! This is a counterfactual model, not a replay of RTL execution. Request
//! releases and dependencies are supplied by the workload; no protocol,
//! arbitration, cache coherence, data values, or physical timing is inferred.

use serde::{Deserialize, Serialize};
use std::collections::{BTreeMap, BTreeSet};

/// Workload and candidate memories evaluated using the same request graph.
#[derive(Clone, Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct MemoryExperiment {
    /// Independent memory requests and their explicit causal dependencies.
    pub requests: Vec<MemoryRequest>,
    /// Alternative memory organizations.
    pub candidates: Vec<MemoryCandidate>,
}

/// One access; a dependency must complete before this request is released.
#[derive(Clone, Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct MemoryRequest {
    /// Unique, stable request identifier.
    pub id: String,
    /// Source of the access, used for reporting only.
    pub requester: String,
    /// Inclusive byte address.
    pub address: u64,
    /// Number of consecutive bytes; accesses may not cross a stripe.
    pub bytes: u64,
    /// Earliest request cycle, independent of predecessor completion.
    pub release: u64,
    /// IDs of requests that must complete before this one can be issued.
    #[serde(default)]
    pub depends_on: Vec<String>,
    /// Optional absolute completion deadline, inclusive.
    pub deadline: Option<u64>,
}

/// Uniform striped memory with independent banks and pipelined ports.
#[derive(Clone, Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct MemoryCandidate {
    /// Unique candidate name.
    pub name: String,
    /// Total addressable bytes across all banks.
    pub capacity_bytes: u64,
    /// Number of banks, mapped by `(address / stripe_bytes) % banks`.
    pub banks: usize,
    /// Size of an address stripe in bytes.
    pub stripe_bytes: u64,
    /// Identical ports per bank, shared by all access types.
    pub ports_per_bank: usize,
    /// Cycles from service start to completion, at least one.
    pub latency: u64,
    /// Minimum cycles between service starts on each port, at least one.
    pub initiation_interval: u64,
}

/// Timing result for a single memory request.
#[derive(Clone, Debug, Serialize)]
pub struct MemoryAccessResult {
    /// Request identifier.
    pub id: String,
    /// Requesting agent.
    pub requester: String,
    /// Selected bank.
    pub bank: usize,
    /// Cycle at which release and dependencies are both satisfied.
    pub ready: u64,
    /// Cycle a port begins service.
    pub start: u64,
    /// Cycle the request completes.
    pub completion: u64,
    /// Whether an explicit deadline was missed; null if none was supplied.
    pub deadline_missed: Option<bool>,
}

/// Counterfactual timing results for one candidate.
#[derive(Clone, Debug, Serialize)]
pub struct MemoryCandidateResult {
    /// Candidate name.
    pub name: String,
    /// Last completion cycle, or zero for an empty workload.
    pub completion_cycle: u64,
    /// Sum of time spent waiting for bank ports after becoming ready.
    pub total_queue_cycles: u64,
    /// Number of explicit deadlines missed.
    pub deadlines_missed: usize,
    /// Results in service arbitration order.
    pub accesses: Vec<MemoryAccessResult>,
}

impl MemoryExperiment {
    /// Evaluate all candidates or reject invalid/ambiguous input.
    ///
    /// Among dependency-ready requests the earliest ready cycle wins; ties are
    /// broken by request ID. Each bank independently assigns the earliest free
    /// port, with port index as tie-breaker. Requests cannot be split across
    /// stripes. This provides deterministic FIFO arbitration per bank, not an
    /// optimal schedule. Read/write semantics and same-address ordering must be
    /// expressed by dependencies; this timing model does not check values.
    pub fn evaluate(&self) -> Result<Vec<MemoryCandidateResult>, String> {
        let mut ids = BTreeMap::new();
        for (index, request) in self.requests.iter().enumerate() {
            if request.id.is_empty() || request.requester.is_empty() || request.bytes == 0 {
                return Err("request id, requester and bytes must be nonempty/nonzero".into());
            }
            if ids.insert(request.id.as_str(), index).is_some() {
                return Err(format!("duplicate request id: {}", request.id));
            }
        }
        for request in &self.requests {
            let mut seen = BTreeSet::new();
            for dependency in &request.depends_on {
                if !ids.contains_key(dependency.as_str()) {
                    return Err(format!(
                        "{} depends on unknown request {dependency}",
                        request.id
                    ));
                }
                if !seen.insert(dependency) {
                    return Err(format!("{} repeats dependency {dependency}", request.id));
                }
            }
        }
        let mut names = BTreeSet::new();
        let mut results = Vec::new();
        for candidate in &self.candidates {
            if candidate.name.is_empty() || !names.insert(&candidate.name) {
                return Err(format!(
                    "empty or duplicate candidate name: {}",
                    candidate.name
                ));
            }
            if candidate.banks == 0
                || candidate.ports_per_bank == 0
                || candidate.stripe_bytes == 0
                || candidate.capacity_bytes == 0
                || candidate.latency == 0
                || candidate.initiation_interval == 0
            {
                return Err(format!(
                    "{}: memory parameters must be nonzero",
                    candidate.name
                ));
            }
            let port_count = candidate
                .banks
                .checked_mul(candidate.ports_per_bank)
                .filter(|count| *count <= 1_000_000)
                .ok_or("too many memory ports")?;
            let mut bank_for = Vec::new();
            for request in &self.requests {
                let end = request
                    .address
                    .checked_add(request.bytes - 1)
                    .ok_or_else(|| format!("{}: address overflow", request.id))?;
                if end >= candidate.capacity_bytes {
                    return Err(format!(
                        "{}: request {} exceeds memory capacity",
                        candidate.name, request.id
                    ));
                }
                if request.address / candidate.stripe_bytes != end / candidate.stripe_bytes {
                    return Err(format!(
                        "{}: request {} crosses a stripe",
                        candidate.name, request.id
                    ));
                }
                bank_for.push(
                    (request.address / candidate.stripe_bytes % candidate.banks as u64) as usize,
                );
            }
            let mut available = vec![0u64; port_count];
            let mut completion = vec![None; self.requests.len()];
            let mut accesses = Vec::new();
            let mut total_queue_cycles = 0u64;
            while accesses.len() < self.requests.len() {
                let next = self
                    .requests
                    .iter()
                    .enumerate()
                    .filter_map(|(index, request)| {
                        if completion[index].is_some() {
                            return None;
                        }
                        let mut ready = request.release;
                        for dependency in &request.depends_on {
                            ready = ready.max(completion[ids[dependency.as_str()]]?);
                        }
                        Some((ready, request.id.as_str(), index))
                    })
                    .min();
                let Some((ready, _, index)) = next else {
                    return Err("request dependencies contain a cycle".into());
                };
                let request = &self.requests[index];
                let bank = bank_for[index];
                let base = bank * candidate.ports_per_bank;
                let port = (base..base + candidate.ports_per_bank)
                    .min_by_key(|&port| (available[port], port))
                    .expect("at least one port");
                let start = ready.max(available[port]);
                let done = start
                    .checked_add(candidate.latency)
                    .ok_or("completion cycle overflow")?;
                available[port] = start
                    .checked_add(candidate.initiation_interval)
                    .ok_or("port cycle overflow")?;
                total_queue_cycles = total_queue_cycles
                    .checked_add(start - ready)
                    .ok_or("queue cycle overflow")?;
                completion[index] = Some(done);
                accesses.push(MemoryAccessResult {
                    id: request.id.clone(),
                    requester: request.requester.clone(),
                    bank,
                    ready,
                    start,
                    completion: done,
                    deadline_missed: request.deadline.map(|deadline| done > deadline),
                });
            }
            results.push(MemoryCandidateResult {
                name: candidate.name.clone(),
                completion_cycle: accesses.iter().map(|a| a.completion).max().unwrap_or(0),
                total_queue_cycles,
                deadlines_missed: accesses
                    .iter()
                    .filter(|a| a.deadline_missed == Some(true))
                    .count(),
                accesses,
            });
        }
        if self.candidates.is_empty() {
            return Err("at least one candidate is required".into());
        }
        Ok(results)
    }
}
