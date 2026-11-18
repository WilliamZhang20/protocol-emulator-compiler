//! Protocol-control graph representation and mapping to the 8x4 actors.
//!
//! This is a deliberately small first mapping target. The RTL actor has four
//! states, observes one signal-space line, and can emit one of three action
//! pulses on each transition. Unreachable and behaviorally equivalent source
//! states are removed before fitting; remaining oversized graphs are rejected.

/// Maximum states in one configured protocol actor.
pub const ACTOR_STATES: usize = 4;
/// Number of protocol actors in the 8x4 fabric.
pub const ACTORS: usize = 2;
/// Number of one-hot action outputs per actor.
pub const ACTOR_ACTIONS: usize = 3;
/// Maximum source states accepted before reachability pruning and minimization.
pub const MAX_LOGICAL_STATES: usize = 256;

/// A protocol control graph before assigning a physical actor.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct ProtocolGraph {
    /// Human-readable graph name.
    pub name: String,
    /// The signal-space line sampled by the actor, from 0 through 63.
    pub input_line: u8,
    /// Number of logical states, from 1 through 4 for actor mapping.
    pub states: usize,
    /// State in which the actor starts after reset or disable.
    pub initial_state: usize,
    /// State/input transitions.
    pub transitions: Vec<Transition>,
}

/// One transition on the selected input line's low or high value.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Transition {
    /// Source state.
    pub from: usize,
    /// Input value: false is low, true is high.
    pub input_high: bool,
    /// Destination state.
    pub to: usize,
    /// Optional one-based action output, 1 through 3.
    pub action: Option<usize>,
}

/// Why a protocol graph cannot use a spatial actor.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum ActorIneligible {
    /// Actor selection accepts only the 64 signal-space lines.
    InputLineOutOfRange(u8),
    /// The actor has four states.
    TooManyStates(usize),
    /// No state was provided.
    NoStates,
    /// The selected initial state does not exist.
    InvalidInitialState(usize),
    /// The source graph exceeds the mapper's bounded state-table size.
    GraphTooLarge(usize),
    /// A transition refers to a nonexistent state.
    InvalidTransitionState {
        /// Invalid source state.
        from: usize,
        /// Invalid destination state.
        to: usize,
    },
    /// One state cannot contain two transitions for the same input value.
    DuplicateInput {
        /// State with a duplicate row.
        state: usize,
        /// Input value with a duplicate row.
        input_high: bool,
    },
    /// The actor offers exactly three action outputs.
    InvalidAction(usize),
    /// The requested physical actor does not exist.
    InvalidActor(usize),
}

impl std::fmt::Display for ActorIneligible {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::InputLineOutOfRange(line) => write!(f, "signal line {line} is outside 0..63"),
            Self::TooManyStates(states) => {
                write!(f, "{states} states exceed the actor's four-state capacity")
            }
            Self::NoStates => f.write_str("the graph has no states"),
            Self::InvalidInitialState(state) => write!(f, "initial state {state} does not exist"),
            Self::GraphTooLarge(states) => write!(
                f,
                "{states} source states exceed the mapper limit of {MAX_LOGICAL_STATES}"
            ),
            Self::InvalidTransitionState { from, to } => {
                write!(f, "transition {from} -> {to} refers to a nonexistent state")
            }
            Self::DuplicateInput { state, input_high } => write!(
                f,
                "state {state} has two transitions for input {}",
                if *input_high { "high" } else { "low" }
            ),
            Self::InvalidAction(action) => write!(f, "action {action} is outside 1..=3"),
            Self::InvalidActor(actor) => write!(f, "actor {actor} does not exist"),
        }
    }
}

/// A concrete mapping to one actor and its configuration writes.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct ActorMapping {
    /// Actor index, 0 or 1.
    pub actor: usize,
    /// Source states before unreachable-state removal and minimization.
    pub source_states: usize,
    /// States remaining in the mapped actor after minimization.
    pub mapped_states: usize,
    /// Source states removed or merged by the optimizer.
    pub states_reduced: usize,
    /// Explicit source transition rows remaining after minimization.
    pub mapped_transitions: usize,
    /// Selector and transition-table configuration writes.
    pub writes: Vec<(u8, u16)>,
    /// Number of unconfigured state/input rows, which hold their current state.
    pub implicit_hold_rows: usize,
    /// Output signal-space lines for action 1, 2, and 3.
    pub action_lines: [u8; ACTOR_ACTIONS],
}

/// Map a graph onto the requested 8x4 protocol actor.
///
/// Rows not present in `transitions` hold the current state and emit no action,
/// matching the actor RTL reset contents. The returned writes use config
/// addresses `0x90..=0x9f` and selectors `0xa0..=0xa1`.
pub fn map_actor(graph: &ProtocolGraph, actor: usize) -> Result<ActorMapping, ActorIneligible> {
    let source_states = graph.states;
    if graph.input_line >= 64 {
        return Err(ActorIneligible::InputLineOutOfRange(graph.input_line));
    }
    if graph.states == 0 {
        return Err(ActorIneligible::NoStates);
    }
    if graph.states > MAX_LOGICAL_STATES {
        return Err(ActorIneligible::GraphTooLarge(graph.states));
    }
    if graph.initial_state >= graph.states {
        return Err(ActorIneligible::InvalidInitialState(graph.initial_state));
    }
    if actor >= ACTORS {
        return Err(ActorIneligible::InvalidActor(actor));
    }

    let graph = minimize(graph)?;
    let mapped_transitions = graph.transitions.len();
    if graph.states > ACTOR_STATES {
        return Err(ActorIneligible::TooManyStates(graph.states));
    }

    let mut physical_state = [usize::MAX; ACTOR_STATES];
    physical_state[graph.initial_state] = 0;
    let mut next_state = 1;
    for (state, physical) in physical_state.iter_mut().enumerate().take(graph.states) {
        if state != graph.initial_state {
            *physical = next_state;
            next_state += 1;
        }
    }
    let mut rows: [Option<Transition>; ACTOR_STATES * 2] = [None; ACTOR_STATES * 2];
    for transition in &graph.transitions {
        if transition.from >= graph.states || transition.to >= graph.states {
            return Err(ActorIneligible::InvalidTransitionState {
                from: transition.from,
                to: transition.to,
            });
        }
        if let Some(action) = transition.action
            && !(1..=ACTOR_ACTIONS).contains(&action)
        {
            return Err(ActorIneligible::InvalidAction(action));
        }
        let row = physical_state[transition.from] * 2 + usize::from(transition.input_high);
        if rows[row].is_some() {
            return Err(ActorIneligible::DuplicateInput {
                state: transition.from,
                input_high: transition.input_high,
            });
        }
        rows[row] = Some(Transition {
            from: physical_state[transition.from],
            input_high: transition.input_high,
            to: physical_state[transition.to],
            action: transition.action,
        });
    }

    let table_base = 0x90 + (actor * 8) as u8;
    let mut writes = Vec::with_capacity(9);
    writes.push((0xa0 + actor as u8, u16::from(graph.input_line)));
    let mut implicit_hold_rows = 0;
    for (row, transition) in rows.iter().enumerate() {
        let value = if let Some(transition) = transition {
            let action = transition.action.map_or(0, |n| 1 << (n + 1));
            (transition.to as u16) | action
        } else {
            implicit_hold_rows += 1;
            (row / 2) as u16
        };
        writes.push((table_base + row as u8, value));
    }

    Ok(ActorMapping {
        actor,
        source_states,
        mapped_states: graph.states,
        states_reduced: source_states - graph.states,
        mapped_transitions,
        writes,
        implicit_hold_rows,
        action_lines: [
            26 + (actor * 3) as u8,
            27 + (actor * 3) as u8,
            28 + (actor * 3) as u8,
        ],
    })
}

fn minimize(graph: &ProtocolGraph) -> Result<ProtocolGraph, ActorIneligible> {
    use std::collections::{HashMap, VecDeque};

    let mut rows = vec![[None; 2]; graph.states];
    for transition in &graph.transitions {
        if transition.from >= graph.states || transition.to >= graph.states {
            return Err(ActorIneligible::InvalidTransitionState {
                from: transition.from,
                to: transition.to,
            });
        }
        if let Some(action) = transition.action
            && !(1..=ACTOR_ACTIONS).contains(&action)
        {
            return Err(ActorIneligible::InvalidAction(action));
        }
        let input = usize::from(transition.input_high);
        if rows[transition.from][input].is_some() {
            return Err(ActorIneligible::DuplicateInput {
                state: transition.from,
                input_high: transition.input_high,
            });
        }
        rows[transition.from][input] = Some(*transition);
    }

    let effective =
        |state: usize, input: usize| rows[state][input].map_or((state, None), |t| (t.to, t.action));
    let mut reachable = vec![false; graph.states];
    let mut queue = VecDeque::from([graph.initial_state]);
    reachable[graph.initial_state] = true;
    while let Some(state) = queue.pop_front() {
        for input in 0..2 {
            let (next, _) = effective(state, input);
            if !reachable[next] {
                reachable[next] = true;
                queue.push_back(next);
            }
        }
    }
    let active: Vec<_> = (0..graph.states)
        .filter(|&state| reachable[state])
        .collect();

    // Start by separating states with different observable actions. Refine
    // with destination classes until all states have stable behavior.
    let mut classes = vec![usize::MAX; graph.states];
    let mut initial_signatures = HashMap::<Vec<usize>, usize>::new();
    for &state in &active {
        let signature = (0..2)
            .map(|input| effective(state, input).1.unwrap_or(0))
            .collect::<Vec<_>>();
        let next_class = initial_signatures.len();
        let class = *initial_signatures.entry(signature).or_insert(next_class);
        classes[state] = class;
    }

    loop {
        let mut signatures = HashMap::<Vec<usize>, usize>::new();
        let mut refined = vec![usize::MAX; graph.states];
        for &state in &active {
            let mut signature = Vec::with_capacity(4);
            for input in 0..2 {
                let (next, action) = effective(state, input);
                signature.push(action.unwrap_or(0));
                signature.push(classes[next]);
            }
            let next_class = signatures.len();
            let class = *signatures.entry(signature).or_insert(next_class);
            refined[state] = class;
        }
        if refined == classes {
            break;
        }
        classes = refined;
    }

    let class_count = active
        .iter()
        .map(|&state| classes[state])
        .max()
        .map_or(0, |maximum| maximum + 1);
    let mut representative = vec![usize::MAX; class_count];
    for &state in &active {
        representative[classes[state]] = state;
    }
    let mut transitions = Vec::new();
    for (class, &state) in representative.iter().enumerate() {
        for input in 0..2 {
            let (next, action) = effective(state, input);
            let next_class = classes[next];
            if next_class != class || action.is_some() {
                transitions.push(Transition {
                    from: class,
                    input_high: input != 0,
                    to: next_class,
                    action,
                });
            }
        }
    }
    Ok(ProtocolGraph {
        name: graph.name.clone(),
        input_line: graph.input_line,
        states: class_count,
        initial_state: classes[graph.initial_state],
        transitions,
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    fn source_trace(graph: &ProtocolGraph, input: &[bool]) -> Vec<Option<usize>> {
        let mut state = graph.initial_state;
        input
            .iter()
            .map(|&high| {
                let transition = graph
                    .transitions
                    .iter()
                    .find(|transition| transition.from == state && transition.input_high == high);
                if let Some(transition) = transition {
                    state = transition.to;
                    transition.action
                } else {
                    None
                }
            })
            .collect()
    }

    fn mapped_trace(mapping: &ActorMapping, input: &[bool]) -> Vec<Option<usize>> {
        let base = 0x90 + (mapping.actor * 8) as u8;
        let mut state = 0usize;
        input
            .iter()
            .map(|&high| {
                let row = state * 2 + usize::from(high);
                let value = mapping
                    .writes
                    .iter()
                    .find_map(|(address, value)| (*address == base + row as u8).then_some(*value))
                    .expect("all rows are programmed");
                state = usize::from(value & 0b11);
                let action = (value >> 2) & 0b111;
                (action != 0).then(|| action.trailing_zeros() as usize + 1)
            })
            .collect()
    }

    fn handshake() -> ProtocolGraph {
        ProtocolGraph {
            name: "handshake".into(),
            input_line: 32,
            states: 2,
            initial_state: 0,
            transitions: vec![
                Transition {
                    from: 0,
                    input_high: true,
                    to: 1,
                    action: Some(1),
                },
                Transition {
                    from: 1,
                    input_high: false,
                    to: 0,
                    action: Some(2),
                },
            ],
        }
    }

    #[test]
    fn maps_rows_and_actor_lines_to_hardware_registers() {
        let mapping = map_actor(&handshake(), 1).expect("fits actor");
        assert_eq!(mapping.writes[0], (0xa1, 32));
        assert_eq!(mapping.writes[1], (0x98, 0));
        assert_eq!(mapping.writes[2], (0x99, 1 | 1 << 2));
        assert_eq!(mapping.writes[3], (0x9a, 1 << 3));
        assert_eq!(mapping.action_lines, [29, 30, 31]);
        assert_eq!(mapping.implicit_hold_rows, 6);
    }

    #[test]
    fn rejects_graphs_outside_actor_capabilities() {
        let graph = ProtocolGraph {
            name: "chain".into(),
            input_line: 32,
            states: 5,
            initial_state: 0,
            transitions: (0..5)
                .map(|state| Transition {
                    from: state,
                    input_high: true,
                    to: (state + 1).min(4),
                    action: match state {
                        0 => Some(1),
                        4 => Some(2),
                        _ => None,
                    },
                })
                .collect(),
        };
        assert_eq!(map_actor(&graph, 0), Err(ActorIneligible::TooManyStates(5)));

        let mut graph = handshake();
        graph.input_line = 64;
        assert_eq!(
            map_actor(&graph, 0),
            Err(ActorIneligible::InputLineOutOfRange(64))
        );
    }

    #[test]
    fn removes_unreachable_and_equivalent_source_states_before_fitting() {
        let graph = ProtocolGraph {
            name: "reducible".into(),
            input_line: 40,
            states: 5,
            initial_state: 0,
            transitions: vec![
                Transition {
                    from: 0,
                    input_high: false,
                    to: 2,
                    action: None,
                },
                Transition {
                    from: 0,
                    input_high: true,
                    to: 1,
                    action: Some(1),
                },
                Transition {
                    from: 1,
                    input_high: false,
                    to: 1,
                    action: None,
                },
                Transition {
                    from: 1,
                    input_high: true,
                    to: 1,
                    action: None,
                },
                Transition {
                    from: 2,
                    input_high: false,
                    to: 2,
                    action: None,
                },
                Transition {
                    from: 2,
                    input_high: true,
                    to: 2,
                    action: None,
                },
            ],
        };
        let mapping = map_actor(&graph, 0).expect("equivalent states merge");
        assert_eq!(mapping.source_states, 5);
        assert_eq!(mapping.mapped_states, 2);
        assert_eq!(mapping.states_reduced, 3);

        // Check every six-sample input sequence against the source graph.
        for word in 0..64 {
            let input: Vec<_> = (0..6).map(|bit| word & (1 << bit) != 0).collect();
            assert_eq!(mapped_trace(&mapping, &input), source_trace(&graph, &input));
        }
    }

    #[test]
    fn rejects_ambiguous_state_input_pairs() {
        let mut graph = handshake();
        graph.transitions.push(Transition {
            from: 0,
            input_high: true,
            to: 0,
            action: None,
        });
        assert_eq!(
            map_actor(&graph, 0),
            Err(ActorIneligible::DuplicateInput {
                state: 0,
                input_high: true
            })
        );
    }
}
