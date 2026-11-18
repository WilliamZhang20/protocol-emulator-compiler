//! Protocol-control graph representation and mapping to the 8x4 actors.
//!
//! This is a deliberately small first mapping target. The RTL actor has four
//! states, observes one signal-space line, and can emit one of three action
//! pulses on each transition. Graphs outside that shape remain eligible for
//! the PX8 controller.

/// Maximum states in one configured protocol actor.
pub const ACTOR_STATES: usize = 4;
/// Number of protocol actors in the 8x4 fabric.
pub const ACTORS: usize = 2;
/// Number of one-hot action outputs per actor.
pub const ACTOR_ACTIONS: usize = 3;

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
    if graph.input_line >= 64 {
        return Err(ActorIneligible::InputLineOutOfRange(graph.input_line));
    }
    if graph.states == 0 {
        return Err(ActorIneligible::NoStates);
    }
    if graph.states > ACTOR_STATES {
        return Err(ActorIneligible::TooManyStates(graph.states));
    }
    if graph.initial_state >= graph.states {
        return Err(ActorIneligible::InvalidInitialState(graph.initial_state));
    }
    if actor >= ACTORS {
        return Err(ActorIneligible::InvalidActor(actor));
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
        writes,
        implicit_hold_rows,
        action_lines: [
            26 + (actor * 3) as u8,
            27 + (actor * 3) as u8,
            28 + (actor * 3) as u8,
        ],
    })
}

#[cfg(test)]
mod tests {
    use super::*;

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
        let mut graph = handshake();
        graph.states = 5;
        assert_eq!(map_actor(&graph, 0), Err(ActorIneligible::TooManyStates(5)));

        let mut graph = handshake();
        graph.input_line = 64;
        assert_eq!(
            map_actor(&graph, 0),
            Err(ActorIneligible::InputLineOutOfRange(64))
        );
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
