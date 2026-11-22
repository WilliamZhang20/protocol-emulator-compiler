//! Target-independent synchronous protocol state machines.
//!
//! This IR records logical inputs and behavior before signal-space assignment.
//! The existing backend currently supports at most two one-bit inputs and three
//! actions; these are language limits, not properties of spatial computation.
use crate::diagnostic::{Error, Result};
use crate::syntax::ast::{Expr, ExprKind, Item, Name, ProtocolTransition};
use crate::value::Env;
use serde::Serialize;
use std::collections::{BTreeMap, BTreeSet, VecDeque};

/// Extract protocol state machines without allocating target hardware.
///
/// Constants and clock values are evaluated, and protocol rows are checked
/// for duplicate inputs. Other declarations and signal references are not
/// type-checked here. Successful extraction does not mean a target can compile
/// the program. This makes over-capacity workloads available to modelers.
pub fn extract_logical_protocols(source: &str) -> Result<Vec<LogicalProtocol>> {
    let items = crate::syntax::parse(source)?;
    extract(&items)
}

pub(crate) fn extract(items: &[Item]) -> Result<Vec<LogicalProtocol>> {
    let env = environment(items)?;
    extract_with_env(items, &env)
}

pub(crate) fn environment(items: &[Item]) -> Result<Env> {
    let clock_hz = match items.iter().find_map(|item| match item {
        Item::Clock(expr) => Some(expr),
        _ => None,
    }) {
        Some(expr) => crate::elaborate::clock(expr)?,
        None => 50_000_000,
    };
    let mut env = Env {
        clock_hz,
        ..Env::default()
    };
    for item in items {
        if let Item::Const { name, value } = item {
            let evaluated = env.eval(value)?;
            env.defs.insert(name.text.clone(), value.span);
            env.consts.insert(name.text.clone(), evaluated);
        }
    }
    Ok(env)
}

pub(crate) fn extract_with_env(items: &[Item], env: &Env) -> Result<Vec<LogicalProtocol>> {
    items
        .iter()
        .filter_map(|item| match item {
            Item::Protocol {
                name,
                input,
                input2,
                transitions,
            } => Some(lower(name, input, input2.as_ref(), transitions, env)),
            _ => None,
        })
        .collect()
}

/// A one-bit source as named by a Spatial program, before physical binding.
#[derive(Clone, Debug, PartialEq, Eq, Serialize)]
#[serde(tag = "kind", rename_all = "snake_case")]
pub enum LogicalSignal {
    /// A constant zero or one.
    Constant {
        /// The constant value.
        value: bool,
    },
    /// A named resource's default output or an explicitly named output.
    Resource {
        /// Name of the resource in the source program.
        name: String,
        /// Output name, or none for a resource's default signal.
        output: Option<String>,
    },
}

/// A state transition evaluated on one clock edge.
#[derive(Clone, Debug, PartialEq, Eq, Serialize)]
pub struct LogicalTransition {
    /// Source state identifier.
    pub from: usize,
    /// Values of all inputs, in declaration order.
    pub inputs: Vec<bool>,
    /// Destination state identifier.
    pub to: usize,
    /// One-based action identifier, if this transition emits a pulse.
    pub action: Option<usize>,
}

/// Synchronous Mealy machine, independent of a physical actor or controller.
#[derive(Clone, Debug, PartialEq, Eq, Serialize)]
pub struct LogicalProtocol {
    /// Source declaration name.
    pub name: String,
    /// Ordered one-bit inputs sampled on each active cycle.
    pub inputs: Vec<LogicalSignal>,
    /// Number of states, including unreferenced gaps in state numbering.
    pub states: usize,
    /// Reset state.
    pub initial_state: usize,
    /// Explicit rows; all unspecified input combinations hold state silently.
    pub transitions: Vec<LogicalTransition>,
}

/// The outcome of one synchronous state-machine step.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct ProtocolStep {
    /// State after the clock edge.
    pub state: usize,
    /// Action pulse emitted on that edge, if any.
    pub action: Option<usize>,
}

/// Reachability and behaviorally equivalent state classes of a logical machine.
#[derive(Clone, Debug, PartialEq, Eq, Serialize)]
pub struct ProtocolAnalysis {
    /// States reachable from reset, in ascending identifier order.
    pub reachable_states: Vec<usize>,
    /// Reachable state classes with identical future action behavior.
    pub equivalence_classes: Vec<Vec<usize>>,
    /// Declared states not reachable from reset, including numbering gaps.
    pub unreachable_state_count: usize,
}

/// Capabilities of a hypothetical protocol-control tile.
#[derive(Clone, Debug)]
pub struct ProtocolCandidate {
    /// Number of one-bit inputs supported.
    pub max_inputs: usize,
    /// Available local states after graph minimization.
    pub max_states: usize,
    /// Maximum one-based action identifier supported.
    pub max_action: usize,
    /// Input-sample-to-action latency in cycles, if known.
    pub response_cycles: Option<u64>,
    /// Maximum allowed input-sample-to-action latency, if specified.
    pub response_deadline_cycles: Option<u64>,
}

/// Assessment of one protocol on an abstract tile, excluding integration costs.
#[derive(Clone, Debug, PartialEq, Eq, Serialize)]
pub struct ProtocolCandidateAssessment {
    /// Whether all modeled capacity constraints pass.
    pub capacity_fits: bool,
    /// Each failed capacity requirement, in deterministic order.
    pub capacity_failures: Vec<String>,
    /// Whether the timing constraint passes; null when unspecified or unknown.
    pub response_deadline_met: Option<bool>,
    /// Reachability and minimized-state results used by this assessment.
    pub analysis: ProtocolAnalysis,
}

impl LogicalProtocol {
    /// Validate the machine before interpreting or analyzing externally
    /// constructed values. Up to eight input bits are supported by analysis.
    pub fn validate(&self) -> std::result::Result<(), &'static str> {
        if self.inputs.is_empty() || self.inputs.len() > 8 {
            return Err("protocol analysis requires one to eight inputs");
        }
        if self.states == 0 || self.initial_state >= self.states {
            return Err("invalid protocol initial state");
        }
        let mut seen = BTreeSet::new();
        for row in &self.transitions {
            if row.from >= self.states || row.to >= self.states {
                return Err("protocol transition state is out of range");
            }
            if row.inputs.len() != self.inputs.len() {
                return Err("incorrect protocol input count");
            }
            if row.action == Some(0) {
                return Err("protocol action must be positive");
            }
            if !seen.insert((row.from, row.inputs.clone())) {
                return Err("duplicate protocol transition");
            }
        }
        Ok(())
    }

    /// Find reachable states and refine Mealy-machine equivalence classes.
    /// Unspecified rows hold state and produce no action. Only reachable
    /// states are visited, so gaps in source state identifiers do not allocate
    /// a correspondingly large table.
    pub fn analyze(&self) -> std::result::Result<ProtocolAnalysis, &'static str> {
        self.validate()?;
        let rows: BTreeMap<_, _> = self
            .transitions
            .iter()
            .map(|row| {
                let input = row
                    .inputs
                    .iter()
                    .enumerate()
                    .fold(0usize, |mask, (i, bit)| mask | (usize::from(*bit) << i));
                ((row.from, input), (row.to, row.action))
            })
            .collect();
        let width = 1usize << self.inputs.len();
        let next = |state, input| rows.get(&(state, input)).copied().unwrap_or((state, None));
        let mut reachable = BTreeSet::from([self.initial_state]);
        let mut queue = VecDeque::from([self.initial_state]);
        while let Some(state) = queue.pop_front() {
            for input in 0..width {
                let (destination, _) = next(state, input);
                if reachable.insert(destination) {
                    queue.push_back(destination);
                }
            }
        }
        let mut classes: BTreeMap<usize, usize> =
            reachable.iter().map(|&state| (state, 0)).collect();
        loop {
            let mut signatures = BTreeMap::new();
            let mut refined = BTreeMap::new();
            for &state in &reachable {
                let signature: Vec<_> = (0..width)
                    .map(|input| {
                        let (destination, action) = next(state, input);
                        (action, classes[&destination])
                    })
                    .collect();
                let next_class = signatures.len();
                let class = *signatures.entry(signature).or_insert(next_class);
                refined.insert(state, class);
            }
            if refined == classes {
                break;
            }
            classes = refined;
        }
        let count = classes.values().max().map_or(0, |x| x + 1);
        let mut equivalence_classes = vec![Vec::new(); count];
        for (&state, &class) in &classes {
            equivalence_classes[class].push(state);
        }
        Ok(ProtocolAnalysis {
            unreachable_state_count: self.states - reachable.len(),
            reachable_states: reachable.into_iter().collect(),
            equivalence_classes,
        })
    }

    /// Compare this machine with one tile's declared capacity and optional
    /// response latency. This is not a placement, routing, or throughput proof.
    pub fn assess(
        &self,
        candidate: &ProtocolCandidate,
    ) -> std::result::Result<ProtocolCandidateAssessment, &'static str> {
        let analysis = self.analyze()?;
        let mut failures = Vec::new();
        if self.inputs.len() > candidate.max_inputs {
            failures.push(format!(
                "{} inputs exceed {}",
                self.inputs.len(),
                candidate.max_inputs
            ));
        }
        if analysis.equivalence_classes.len() > candidate.max_states {
            failures.push(format!(
                "{} minimized states exceed {}",
                analysis.equivalence_classes.len(),
                candidate.max_states
            ));
        }
        let reachable: BTreeSet<_> = analysis.reachable_states.iter().copied().collect();
        let max_action = self
            .transitions
            .iter()
            .filter(|row| reachable.contains(&row.from))
            .filter_map(|row| row.action)
            .max()
            .unwrap_or(0);
        if max_action > candidate.max_action {
            failures.push(format!(
                "action {max_action} exceeds {}",
                candidate.max_action
            ));
        }
        Ok(ProtocolCandidateAssessment {
            capacity_fits: failures.is_empty(),
            capacity_failures: failures,
            response_deadline_met: candidate
                .response_deadline_cycles
                .zip(candidate.response_cycles)
                .map(|(deadline, delay)| delay <= deadline),
            analysis,
        })
    }
    /// Evaluate a clock edge. Missing rows hold state and emit no action.
    /// Returns an error for an invalid state or wrong input width.
    pub fn step(
        &self,
        state: usize,
        inputs: &[bool],
    ) -> std::result::Result<ProtocolStep, &'static str> {
        self.validate()?;
        if state >= self.states {
            return Err("state is outside the protocol");
        }
        if inputs.len() != self.inputs.len() {
            return Err("incorrect protocol input count");
        }
        let row = self
            .transitions
            .iter()
            .find(|row| row.from == state && row.inputs == inputs);
        Ok(match row {
            Some(row) => ProtocolStep {
                state: row.to,
                action: row.action,
            },
            None => ProtocolStep {
                state,
                action: None,
            },
        })
    }
}

fn signal(expr: &Expr) -> Result<LogicalSignal> {
    match &expr.kind {
        ExprKind::Int(0) => Ok(LogicalSignal::Constant { value: false }),
        ExprKind::Int(1) => Ok(LogicalSignal::Constant { value: true }),
        ExprKind::Name(name) => Ok(LogicalSignal::Resource {
            name: name.clone(),
            output: None,
        }),
        ExprKind::Field(object, output) => match &object.kind {
            ExprKind::Name(name) => Ok(LogicalSignal::Resource {
                name: name.clone(),
                output: Some(output.text.clone()),
            }),
            _ => Err(Error::new(
                expr.span,
                "a protocol input must be a named signal or constant 0/1",
            )),
        },
        _ => Err(Error::new(
            expr.span,
            "a protocol input must be a named signal or constant 0/1",
        )),
    }
}

pub(crate) fn lower(
    name: &Name,
    input: &Expr,
    input2: Option<&Expr>,
    source: &[ProtocolTransition],
    env: &Env,
) -> Result<LogicalProtocol> {
    let mut inputs = vec![signal(input)?];
    if let Some(input) = input2 {
        inputs.push(signal(input)?);
    }
    let mut rows = Vec::with_capacity(source.len());
    let mut states = 1usize;
    let mut seen = BTreeSet::new();
    for transition in source {
        let state = |expr: &Expr| -> Result<usize> {
            usize::try_from(env.integer(expr)?)
                .ok()
                .and_then(|n| n.checked_add(1).map(|_| n))
                .ok_or_else(|| {
                    Error::new(expr.span, "protocol state must be a nonnegative integer")
                })
        };
        let from = state(&transition.from)?;
        let to = state(&transition.to)?;
        states = states.max(from + 1).max(to + 1);
        let action = transition
            .action
            .as_ref()
            .map(|expr| {
                usize::try_from(env.integer(expr)?)
                    .ok()
                    .filter(|&n| n > 0)
                    .ok_or_else(|| {
                        Error::new(expr.span, "protocol action must be a positive integer")
                    })
            })
            .transpose()?;
        let mut values = vec![transition.input_high];
        if input2.is_some() {
            values.push(transition.input2_high);
        }
        if !seen.insert((from, values.clone())) {
            return Err(Error::new(
                name.span,
                format!(
                    "state {from} has two transitions for input {}{}",
                    if transition.input_high { "high" } else { "low" },
                    input2.map_or(String::new(), |_| format!(
                        ", second input {}",
                        if transition.input2_high {
                            "high"
                        } else {
                            "low"
                        }
                    ))
                ),
            ));
        }
        rows.push(LogicalTransition {
            from,
            inputs: values,
            to,
            action,
        });
    }
    Ok(LogicalProtocol {
        name: name.text.clone(),
        inputs,
        states,
        initial_state: 0,
        transitions: rows,
    })
}
