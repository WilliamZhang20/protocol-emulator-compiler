//! Resolve names, assign hardware, and turn declarations into configuration
//! writes. This is where "does it fit on the chip?" is decided.
use crate::ResourceAssignment;
use crate::control::{self, Exports};
use crate::diagnostic::{Error, Result, Span};
use crate::hardware::{self, Line, PinMode, config};
use crate::logic::{self, Formula};
use crate::px8::Assembler;
use crate::syntax::ast::{Expr, ExprKind, Field, Item, Name, ProtocolTransition, Stmt, StmtKind};
use crate::value::{Env, Value};
use std::collections::{BTreeMap, HashMap};

const DEFAULT_CLOCK_HZ: i128 = 50_000_000;

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub(crate) enum Kind {
    Pin,
    Timer,
    Shifter,
    Counter,
    Reg,
    Event,
    Flag,
    Crc,
    Protocol,
}

impl Kind {
    pub(crate) fn noun(self) -> &'static str {
        match self {
            Kind::Pin => "pin",
            Kind::Timer => "timer",
            Kind::Shifter => "shifter",
            Kind::Counter => "counter",
            Kind::Reg => "reg",
            Kind::Event => "event",
            Kind::Flag => "flag",
            Kind::Crc => "crc",
            Kind::Protocol => "protocol actor",
        }
    }

    /// Signals a declaration of this kind provides, for error hints.
    fn outputs(self) -> &'static str {
        match self {
            Kind::Pin => "`.rise`, `.fall` (pulses) or the pin name itself (its level)",
            Kind::Timer => "`.tick`/`.rise`, `.fall` (pulses) or `.out` (level)",
            Kind::Shifter => "`.word`, `.done` (pulses), `.out` or `.busy` (levels)",
            Kind::Counter => "`.reached` (pulse) or `.at_least` (level)",
            Kind::Crc => "`.out` (its next check bit) or `.ok` (the check matches), both levels",
            Kind::Reg | Kind::Event | Kind::Flag => "its name",
            Kind::Protocol => "`.action1`, `.action2` or `.action3` (pulses)",
        }
    }

    fn capacity(self) -> (usize, &'static str) {
        match self {
            Kind::Pin => (hardware::PADS, "pins"),
            Kind::Timer => (hardware::TIMERS, "timers"),
            Kind::Shifter => (hardware::SHIFTERS, "shifters"),
            Kind::Counter => (hardware::COUNTERS, "counters"),
            Kind::Reg => (hardware::LOGIC_CELLS, "logic cells (`reg`s)"),
            Kind::Event => (hardware::EVENTS, "events"),
            Kind::Flag => (hardware::FLAGS, "flags"),
            Kind::Crc => (hardware::CRCS, "crc"),
            Kind::Protocol => (crate::protocol::ACTORS, "protocol actors"),
        }
    }
}

/// A declared name and the hardware index it was given.
#[derive(Clone, Copy)]
pub(crate) struct Decl {
    pub kind: Kind,
    pub index: usize,
}

/// Which way bytes flow through an exported stream.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub(crate) enum Direction {
    /// Host writes, the chip sends on the wire.
    ToChip,
    /// The chip receives from the wire, the host reads.
    FromChip,
}

#[derive(Clone, Debug)]
pub(crate) struct Stream {
    pub packets: bool,
    pub channel: usize,
    pub direction: Direction,
}

/// A shifter's FIFO connections to the host.
#[derive(Clone, Copy, Default)]
pub(crate) struct Fifo {
    /// Takes words from the host (TX FIFO).
    pub pull: bool,
    /// Gives words to the host (RX FIFO).
    pub push: bool,
}

/// The elaborated program.
pub(crate) struct Design<'a> {
    pub clock_hz: i128,
    /// Constants, for evaluating test values.
    pub env: Env,
    pub image: Vec<u8>,
    /// Pin name to physical `uio` pad.
    pub pins: BTreeMap<String, usize>,
    pub streams: BTreeMap<String, Stream>,
    pub exports: Exports,
    pub usage: Vec<(&'static str, usize, usize)>,
    pub resources: Vec<ResourceAssignment>,
    pub protocol_mappings: Vec<crate::ProtocolMapping>,
    pub protocol_placement_search: crate::ProtocolPlacementSearch,
    pub tests: Vec<(&'a Name, &'a [Stmt], Option<&'a Expr>)>,
    pub invariants: Vec<(&'a Name, &'a Expr)>,
}

/// Names, hardware assignments and tile declarations, shared with the
/// controller lowering so `.set` can re-encode a tile at runtime.
pub(crate) struct Chip<'a> {
    pub env: Env,
    pub decls: HashMap<String, Decl>,
    pub pads: Vec<usize>,
    pub fifos: Vec<Fifo>,
    pub tiles: HashMap<String, &'a [Field]>,
    /// Each pin's declaration, so a program can release a pin and drive it again.
    pub pin_decls: HashMap<String, (&'a Name, &'a Name, Option<&'a Expr>)>,
    /// Physical pad and edge direction to configurable edge-cell index.
    pub edges: HashMap<(usize, bool), usize>,
    /// Action signal lines selected by the protocol mapper. Controller-backed
    /// graphs expose the two PX8 FIRE outputs instead of actor outputs.
    pub protocol_action_lines: HashMap<String, [Option<Line>; 3]>,
}

/// Score actor-to-shifter locality for both possible physical actor orders.
/// Actor `k` sits beside shifter channel `k`; every direct signal connection
/// between a protocol graph and a shifter contributes one local match.
struct ProtocolActorSearch {
    slots: HashMap<String, (usize, usize)>,
    report: crate::ProtocolPlacementSearch,
}

fn protocol_actor_slots(items: &[Item]) -> ProtocolActorSearch {
    fn mentions_protocol_action(expr: &Expr, name: &str) -> bool {
        match &expr.kind {
            ExprKind::Field(object, field) => {
                (matches!(&object.kind, ExprKind::Name(n) if n == name)
                    && matches!(field.text.as_str(), "action1" | "action2" | "action3"))
                    || mentions_protocol_action(object, name)
            }
            ExprKind::Call(callee, args) => {
                mentions_protocol_action(callee, name)
                    || args
                        .iter()
                        .any(|arg| mentions_protocol_action(&arg.value, name))
            }
            ExprKind::List(values) => values
                .iter()
                .any(|value| mentions_protocol_action(value, name)),
            ExprKind::Unary(_, value) => mentions_protocol_action(value, name),
            ExprKind::Binary(_, left, right) => {
                mentions_protocol_action(left, name) || mentions_protocol_action(right, name)
            }
            ExprKind::If(condition, yes, no) => {
                mentions_protocol_action(condition, name)
                    || mentions_protocol_action(yes, name)
                    || mentions_protocol_action(no, name)
            }
            ExprKind::Int(_) | ExprKind::Quantity(_, _) | ExprKind::Str(_) | ExprKind::Name(_) => {
                false
            }
        }
    }

    fn mentions_shifter(expr: &Expr, shifter: &str) -> bool {
        match &expr.kind {
            ExprKind::Field(object, _) => {
                matches!(&object.kind, ExprKind::Name(n) if n == shifter)
                    || mentions_shifter(object, shifter)
            }
            ExprKind::Call(callee, args) => {
                mentions_shifter(callee, shifter)
                    || args.iter().any(|arg| mentions_shifter(&arg.value, shifter))
            }
            ExprKind::List(values) => values.iter().any(|value| mentions_shifter(value, shifter)),
            ExprKind::Unary(_, value) => mentions_shifter(value, shifter),
            ExprKind::Binary(_, left, right) => {
                mentions_shifter(left, shifter) || mentions_shifter(right, shifter)
            }
            ExprKind::If(condition, yes, no) => {
                mentions_shifter(condition, shifter)
                    || mentions_shifter(yes, shifter)
                    || mentions_shifter(no, shifter)
            }
            ExprKind::Int(_) | ExprKind::Quantity(_, _) | ExprKind::Str(_) | ExprKind::Name(_) => {
                false
            }
        }
    }

    let shifters: Vec<_> = items
        .iter()
        .filter_map(|item| match item {
            Item::Tile { kind, name, .. } if kind.text == "shifter" => Some(name.text.as_str()),
            _ => None,
        })
        .take(crate::protocol::ACTORS)
        .collect();
    let protocols: Vec<_> = items
        .iter()
        .filter_map(|item| match item {
            Item::Protocol { name, input, .. } => Some((name.text.clone(), input)),
            _ => None,
        })
        .collect();
    let preferences: Vec<[usize; crate::protocol::ACTORS]> = protocols
        .iter()
        .map(|(protocol, input)| {
            let mut score = [0; crate::protocol::ACTORS];
            for (channel, shifter) in shifters.iter().enumerate() {
                if mentions_shifter(input, shifter) {
                    score[channel] += 1;
                }
                for item in items {
                    if let Item::Tile {
                        kind,
                        name: tile_name,
                        fields,
                    } = item
                        && kind.text == "shifter"
                        && tile_name.text == *shifter
                    {
                        for field in fields {
                            if field
                                .value
                                .as_ref()
                                .is_some_and(|expr| mentions_protocol_action(expr, protocol))
                            {
                                score[channel] += 1;
                            }
                        }
                    }
                }
            }
            score
        })
        .collect();

    let mut assignment = HashMap::new();
    let mut report = crate::ProtocolPlacementSearch::default();
    match protocols.len() {
        0 => {}
        1 => {
            report.candidates = (0..crate::protocol::ACTORS)
                .map(|actor| crate::ProtocolPlacementCandidate {
                    implementation: "spatial actor",
                    feasible: true,
                    assignments: vec![(protocols[0].0.clone(), actor)],
                    local_channel_connections: preferences[0][actor],
                    parallel_actors: 1,
                    controller_step_bytes: None,
                })
                .collect();
            let actor = usize::from(preferences[0][1] > preferences[0][0]);
            report.selected = Some(actor);
            assignment.insert(protocols[0].0.clone(), (actor, preferences[0][actor]));
        }
        _ => {
            let direct = preferences[0][0] + preferences[1][1];
            let swapped = preferences[0][1] + preferences[1][0];
            report.candidates = vec![
                crate::ProtocolPlacementCandidate {
                    implementation: "spatial actor",
                    feasible: true,
                    assignments: vec![(protocols[0].0.clone(), 0), (protocols[1].0.clone(), 1)],
                    local_channel_connections: direct,
                    parallel_actors: 2,
                    controller_step_bytes: None,
                },
                crate::ProtocolPlacementCandidate {
                    implementation: "spatial actor",
                    feasible: true,
                    assignments: vec![(protocols[0].0.clone(), 1), (protocols[1].0.clone(), 0)],
                    local_channel_connections: swapped,
                    parallel_actors: 2,
                    controller_step_bytes: None,
                },
            ];
            let slots = if swapped > direct { [1, 0] } else { [0, 1] };
            report.selected = Some(usize::from(swapped > direct));
            for (i, (name, _)) in protocols.iter().take(crate::protocol::ACTORS).enumerate() {
                assignment.insert(name.clone(), (slots[i], preferences[i][slots[i]]));
            }
            for (i, (name, _)) in protocols.iter().enumerate().skip(crate::protocol::ACTORS) {
                assignment.insert(name.clone(), (i, 0));
            }
        }
    }
    ProtocolActorSearch {
        slots: assignment,
        report,
    }
}

pub(crate) fn elaborate(items: &[Item]) -> Result<Design<'_>> {
    let clock_hz = match items.iter().find_map(|item| match item {
        Item::Clock(expr) => Some(expr),
        _ => None,
    }) {
        Some(expr) => clock(expr)?,
        None => DEFAULT_CLOCK_HZ,
    };
    let mut chip = Chip {
        env: Env {
            clock_hz,
            ..Env::default()
        },
        decls: HashMap::new(),
        pads: Vec::new(),
        fifos: vec![Fifo::default(); hardware::SHIFTERS],
        tiles: HashMap::new(),
        pin_decls: HashMap::new(),
        edges: HashMap::new(),
        protocol_action_lines: HashMap::new(),
    };
    let mut names: HashMap<String, Span> = HashMap::new();
    let mut counts = [0usize; 9];
    let mut actor_search = protocol_actor_slots(items);
    let mut pins = Vec::new();
    let mut tests = Vec::new();
    let mut invariants = Vec::new();
    let mut declare = |name: &Name, kind: Option<Kind>, chip: &mut Chip<'_>| {
        if names.insert(name.text.clone(), name.span).is_some() {
            return Err(Error::new(
                name.span,
                format!("`{}` is already declared", name.text),
            ));
        }
        if let Some(kind) = kind {
            let index = if kind == Kind::Protocol {
                actor_search.slots[&name.text].0
            } else {
                counts[kind as usize]
            };
            counts[kind as usize] += 1;
            let (capacity, plural) = kind.capacity();
            if index >= capacity {
                return Err(Error::new(
                    name.span,
                    format!(
                        "the chip has only {capacity} {plural}; `{}` would be number {}",
                        name.text,
                        index + 1
                    ),
                ));
            }
            chip.decls.insert(name.text.clone(), Decl { kind, index });
        }
        Ok(())
    };

    for item in items {
        match item {
            Item::Clock(_) | Item::Contexts(_) | Item::Context { .. } => {}
            Item::Protocol { name, .. } => {
                declare(name, Some(Kind::Protocol), &mut chip)?;
            }
            Item::Const { name, value } => {
                declare(name, None, &mut chip)?;
                chip.env.defs.insert(name.text.clone(), value.span);
                let value = chip.env.eval(value)?;
                chip.env.consts.insert(name.text.clone(), value);
            }
            Item::Pin {
                name,
                pads,
                mode,
                source,
            } => {
                declare(name, Some(Kind::Pin), &mut chip)?;
                chip.pin_decls
                    .insert(name.text.clone(), (name, mode, source.as_ref()));
                let allowed = match pads {
                    None => (0..hardware::PADS).collect(),
                    Some(list) => list
                        .iter()
                        .map(|e| chip.pad(e))
                        .collect::<Result<Vec<_>>>()?,
                };
                pins.push((name, allowed));
            }
            Item::Tile { kind, name, fields } => {
                let kind = match kind.text.as_str() {
                    "timer" => Kind::Timer,
                    "shifter" => Kind::Shifter,
                    "counter" => Kind::Counter,
                    _ => Kind::Crc,
                };
                declare(name, Some(kind), &mut chip)?;
                chip.tiles.insert(name.text.clone(), fields);
            }
            Item::Reg { name, .. } => declare(name, Some(Kind::Reg), &mut chip)?,
            Item::Event(list) => {
                for name in list {
                    declare(name, Some(Kind::Event), &mut chip)?;
                }
            }
            Item::Flag(list) => {
                for (name, _) in list {
                    declare(name, Some(Kind::Flag), &mut chip)?;
                }
            }
            Item::Stream { name, .. } | Item::Fn { name, .. } => declare(name, None, &mut chip)?,
            Item::Test {
                name,
                body,
                step_timeout,
            } => tests.push((name, body.as_slice(), step_timeout.as_ref())),
            Item::Always { name, condition } => invariants.push((name, condition)),
        }
    }
    chip.pads = assign_pads(&pins)?;
    // Assign edge cells after pad matching, before any configuration or
    // controller lowering resolves an edge signal.
    let mut edge_uses = Vec::new();
    for item in items {
        collect_item_edges(item, &mut edge_uses);
    }
    for (pin, falling, span) in edge_uses {
        let Some(decl) = chip.decls.get(pin) else {
            continue;
        };
        if decl.kind != Kind::Pin {
            continue;
        }
        let pad = chip.pads[decl.index];
        if !chip.edges.contains_key(&(pad, falling)) {
            let index = chip.edges.len();
            if index >= hardware::EDGE_CELLS {
                return Err(Error::new(
                    span,
                    "the chip has only 8 edge cells; use fewer distinct pin rises and falls",
                ));
            }
            chip.edges.insert((pad, falling), index);
        }
    }

    let mut streams = BTreeMap::new();
    for item in items {
        if let Item::Stream {
            name,
            from,
            to,
            packets,
            ..
        } = item
        {
            let mut stream = chip.stream(name, from, to)?;
            stream.packets = *packets;
            streams.insert(name.text.clone(), stream);
        }
    }
    let functions: Vec<&Item> = items
        .iter()
        .filter(|i| matches!(i, Item::Fn { .. }))
        .collect();
    control::claim_fifos(&mut chip, &functions)?;
    let mut byte_writes = Vec::new();
    let mut byte_usage = 0;
    let mut framed_streams = std::collections::HashSet::new();
    let mut processed_streams = std::collections::HashSet::new();
    for item in items {
        if let Item::Stream {
            name,
            stages,
            from,
            to,
            ..
        } = item
        {
            let stream = &streams[&name.text];
            let shifter = if stream.direction == Direction::ToChip {
                to
            } else {
                from
            };
            let (tile, _) = chip.decl(shifter)?;
            let burst = chip.tiles[&tile]
                .iter()
                .find(|field| field.name.text == "burst")
                .map(|field| {
                    field
                        .value
                        .as_ref()
                        .map(|expr| chip.bit(expr))
                        .transpose()
                        .map(|value| value.unwrap_or(1) != 0)
                })
                .transpose()?
                .unwrap_or(false);
            let boundaries = if stream.direction == Direction::ToChip {
                stream.packets
            } else {
                burst
            };
            let pipeline = crate::byte_pipeline::encode(
                &chip,
                stages,
                stream.channel,
                stream.direction,
                boundaries,
            )?;
            byte_usage += stages.len();
            byte_writes.extend(pipeline.writes);
            if pipeline.frames {
                framed_streams.insert(&name.text);
            }
            if pipeline.consumes_eop {
                processed_streams.insert(&name.text);
            }
        }
    }
    for item in items {
        if let Item::Stream {
            name,
            from,
            to,
            packets: true,
            ..
        } = item
        {
            let stream = &streams[&name.text];
            if (stream.direction == Direction::ToChip && processed_streams.contains(&name.text))
                || (stream.direction == Direction::FromChip && framed_streams.contains(&name.text))
            {
                continue;
            }
            let shifter = if stream.direction == Direction::ToChip {
                to
            } else {
                from
            };
            let (tile, _) = chip.decl(shifter)?;
            let fields = chip.tiles[&tile];
            let enabled = |key: &str| -> Result<bool> {
                match fields.iter().find(|f| f.name.text == key) {
                    None => Ok(false),
                    Some(field) => Ok(field
                        .value
                        .as_ref()
                        .map(|expr| chip.bit(expr))
                        .transpose()?
                        .unwrap_or(1)
                        != 0),
                }
            };
            if !enabled("burst")? {
                return Err(Error::new(
                    name.span,
                    "a packet stream needs a shifter with `burst`",
                ));
            }
            if stream.direction == Direction::ToChip && !enabled("end_on_packet")? {
                return Err(Error::new(
                    name.span,
                    "a transmit packet stream needs `end_on_packet`",
                ));
            }
            if stream.direction == Direction::FromChip
                && !enabled("end_on_packet")?
                && !fields.iter().any(|f| f.name.text == "end")
            {
                return Err(Error::new(
                    name.span,
                    "a receive packet stream needs `end` or `end_on_packet`",
                ));
            }
        }
    }

    let mut asm = Assembler::default();
    let selectors: Vec<_> = items
        .iter()
        .filter_map(|item| match item {
            Item::Contexts(expr) => Some(expr),
            _ => None,
        })
        .collect();
    if selectors.len() > 1 {
        return Err(Error::new(
            selectors[1].span,
            "declare only one `contexts` selector",
        ));
    }
    let context_blocks: Vec<_> = items
        .iter()
        .filter_map(|item| match item {
            Item::Context { index, body } => Some((index, body)),
            _ => None,
        })
        .collect();
    if let Some((index, _)) = context_blocks.first()
        && selectors.is_empty()
    {
        return Err(Error::new(
            index.span,
            "a context override needs a `contexts(level)` block",
        ));
    }
    if !selectors.is_empty() {
        // Defaults initialize both banks before any overrides. Existing programs
        // without contexts retain their original byte images and write mask.
        asm.config(config::CONTEXT_WRITE, 3);
    }
    let mut edges: Vec<_> = chip.edges.iter().collect();
    edges.sort_by_key(|(_, index)| **index);
    for (&(pad, falling), &index) in edges {
        asm.config(
            config::edge(index),
            pad as u16 | if falling { 1 << 4 } else { 1 << 3 },
        );
    }
    let mut protocol_mappings = Vec::new();
    let mut protocol_writes = HashMap::<String, Vec<(u8, u16)>>::new();
    let mut protocol_controller = None;
    let protocol_count = items
        .iter()
        .filter(|item| matches!(item, Item::Protocol { .. }))
        .count();
    let has_exported_function = functions
        .iter()
        .any(|item| matches!(item, Item::Fn { exported: true, .. }));
    for item in items {
        let Item::Protocol {
            name,
            input,
            transitions,
        } = item
        else {
            continue;
        };
        let graph = protocol_graph(name, input, transitions, &chip)?;
        let actor = chip.decls[&name.text].index;
        match crate::protocol::map_actor(&graph, actor) {
            Ok(mapping) => {
                chip.protocol_action_lines.insert(
                    name.text.clone(),
                    mapping.action_lines.map(|line| Some(Line(line))),
                );
                protocol_mappings.push(crate::ProtocolMapping {
                    name: name.text.clone(),
                    implementation: "spatial actor",
                    actor: Some(mapping.actor),
                    input_line: graph.input_line,
                    source_states: graph.states,
                    mapped_states: mapping.mapped_states,
                    source_transitions: graph.transitions.len(),
                    mapped_transitions: mapping.mapped_transitions,
                    states_reduced: mapping.states_reduced,
                    local_channel_connections: actor_search.slots[&name.text].1,
                    placement_candidates_evaluated: actor_search.report.candidates.len(),
                    actor_pipeline_cycles: Some(1),
                    controller_step_bytes: None,
                });
                protocol_writes.insert(name.text.clone(), mapping.writes);
            }
            Err(crate::protocol::ActorIneligible::TooManyStates(_)) => {
                let optimized = crate::protocol::minimized(&graph)
                    .map_err(|reason| Error::new(name.span, reason.to_string()))?;
                // FIRE drives P2 and P3, so using either as this graph's input
                // would make its own actions retrigger the controller loop.
                let pulse_input = (2..=25).contains(&optimized.input_line)
                    && !matches!(optimized.input_line, 2 | 3);
                let low_rows_hold = optimized
                    .transitions
                    .iter()
                    .all(|row| row.input_high || (row.to == row.from && row.action.is_none()));
                let actions_fit = optimized
                    .transitions
                    .iter()
                    .all(|row| row.action.is_none_or(|action| action <= 2));
                if protocol_count != 1
                    || has_exported_function
                    || !pulse_input
                    || !low_rows_hold
                    || !actions_fit
                {
                    return Err(Error::new(
                        name.span,
                        format!(
                            "protocol `{}` cannot fit a spatial actor ({} minimized states), and its graph is not eligible for the PX8 controller backend; that backend requires one protocol, no exported functions, a pulse input on P4..P25, hold-only low rows, and at most two actions",
                            name.text, optimized.states
                        ),
                    ));
                }
                for candidate in &mut actor_search.report.candidates {
                    candidate.feasible = false;
                }
                let selected = actor_search.report.candidates.len();
                actor_search
                    .report
                    .candidates
                    .push(crate::ProtocolPlacementCandidate {
                        implementation: "PX8 controller",
                        feasible: true,
                        assignments: Vec::new(),
                        local_channel_connections: 0,
                        parallel_actors: 0,
                        controller_step_bytes: Some(controller_step_bytes(&optimized)),
                    });
                actor_search.report.selected = Some(selected);
                chip.protocol_action_lines.insert(
                    name.text.clone(),
                    [Some(Line::fire(0)), Some(Line::fire(1)), None],
                );
                protocol_controller = Some(optimized.clone());
                protocol_mappings.push(crate::ProtocolMapping {
                    name: name.text.clone(),
                    implementation: "PX8 controller",
                    actor: None,
                    input_line: optimized.input_line,
                    source_states: graph.states,
                    mapped_states: optimized.states,
                    source_transitions: graph.transitions.len(),
                    mapped_transitions: optimized.transitions.len(),
                    states_reduced: graph.states - optimized.states,
                    local_channel_connections: 0,
                    placement_candidates_evaluated: actor_search.report.candidates.len(),
                    actor_pipeline_cycles: None,
                    controller_step_bytes: Some(controller_step_bytes(&optimized)),
                });
            }
            Err(reason) => {
                return Err(Error::new(
                    name.span,
                    format!("protocol `{}` cannot map: {reason}", name.text),
                ));
            }
        }
    }
    for item in items {
        let writes = match item {
            Item::Pin {
                name, mode, source, ..
            } => chip.pin(name, mode, source.as_ref())?,
            Item::Tile { name, fields, .. } => {
                let fields: Vec<&Field> = fields.iter().collect();
                chip.tile(name, &fields)?
            }
            Item::Reg {
                name,
                init,
                on,
                value,
            } => chip.reg(name, init.as_ref(), on.as_ref(), value)?,
            Item::Protocol { name, .. } => protocol_writes.remove(&name.text).unwrap_or_default(),
            _ => Vec::new(),
        };
        for (address, value) in writes {
            asm.config(address, value);
        }
    }
    let mut seen = [false; 2];
    for (index, body) in context_blocks {
        let bank = Chip::ranged(index, chip.env.integer(index)?, 0, 1, "the context")? as usize;
        if seen[bank] {
            return Err(Error::new(
                index.span,
                format!("context {bank} is already defined"),
            ));
        }
        seen[bank] = true;
        asm.config(config::CONTEXT_WRITE, 1 << bank);
        let mut overridden = std::collections::HashSet::new();
        for item in body {
            let (name, writes) = match item {
                Item::Pin {
                    name,
                    pads,
                    mode,
                    source,
                } => {
                    let decl = chip.decls.get(&name.text).ok_or_else(|| {
                        Error::new(
                            name.span,
                            "declare the shared pin before overriding it in a context",
                        )
                    })?;
                    if decl.kind != Kind::Pin || pads.is_some() {
                        return Err(Error::new(
                            name.span,
                            "a context can change pin settings, not its kind or pad assignment",
                        ));
                    }
                    (name, chip.pin(name, mode, source.as_ref())?)
                }
                Item::Tile { kind, name, fields } => {
                    let decl = chip.decls.get(&name.text).ok_or_else(|| {
                        Error::new(
                            name.span,
                            "declare the shared tile before overriding it in a context",
                        )
                    })?;
                    if decl.kind.noun() != kind.text || decl.kind == Kind::Crc {
                        return Err(Error::new(
                            name.span,
                            "only existing timers, shifters and counters have context overrides",
                        ));
                    }
                    let mut merged: Vec<&Field> = chip.tiles[&name.text].iter().collect();
                    let mut field_names = std::collections::HashSet::new();
                    for field in fields {
                        if !field_names.insert(&field.name.text) {
                            return Err(Error::new(field.name.span, "duplicate context field"));
                        }
                        merged.retain(|base| base.name.text != field.name.text);
                        merged.push(field);
                    }
                    (name, chip.tile(name, &merged)?)
                }
                Item::Reg {
                    name,
                    init,
                    on,
                    value,
                } => {
                    let decl = chip.decls.get(&name.text).ok_or_else(|| {
                        Error::new(
                            name.span,
                            "declare the shared reg before overriding it in a context",
                        )
                    })?;
                    if decl.kind != Kind::Reg {
                        return Err(Error::new(name.span, "a context reg must override a reg"));
                    }
                    // Only the formula must be restated. The start value and
                    // update pulse retain their shared settings unless given.
                    let (base_init, base_on) = items
                        .iter()
                        .find_map(|item| match item {
                            Item::Reg {
                                name: base_name,
                                init: base_init,
                                on: base_on,
                                ..
                            } if base_name.text == name.text => Some((base_init, base_on)),
                            _ => None,
                        })
                        .expect("declared reg has a shared definition");
                    (
                        name,
                        chip.reg(
                            name,
                            init.as_ref().or(base_init.as_ref()),
                            on.as_ref().or(base_on.as_ref()),
                            value,
                        )?,
                    )
                }
                _ => {
                    return Err(Error::new(
                        index.span,
                        "a context body only overrides existing pins, timers, shifters, counters or regs",
                    ));
                }
            };
            if !overridden.insert(&name.text) {
                return Err(Error::new(
                    name.span,
                    "resource is overridden twice in this context",
                ));
            }
            for (address, value) in writes {
                asm.config(address, value);
            }
        }
    }
    if let Some(selector) = selectors.first() {
        let line = chip.signal(selector)?;
        if !line.is_level() {
            return Err(Error::new(
                selector.span,
                "a context selector must be a level: a pin, flag or reg, not a pulse",
            ));
        }
        // Runtime .set/.drive/.release operations update both banks.
        asm.config(config::CONTEXT_WRITE, 3);
        asm.config(config::CONTEXT_SELECT, u16::from(line.0) | 0x40);
    }
    for (address, value) in byte_writes {
        asm.config(address, value);
    }
    let mut flag_bits = 0u16;
    let mut initialized = false;
    for item in items {
        if let Item::Flag(flags) = item {
            for (name, init) in flags {
                if let Some(expr) = init {
                    flag_bits |= chip.bit(expr)? << chip.decls[&name.text].index;
                    initialized = true;
                }
            }
        }
    }
    if initialized {
        asm.config(config::FLAGS, flag_bits);
    }
    asm.config(config::ARM, 1);
    let exports = control::lower(&chip, &functions, protocol_controller.as_ref(), &mut asm)?;
    let image = asm
        .assemble()
        .map_err(|message| Error::new(Span::default(), message))?;

    let pins = pins
        .iter()
        .map(|(name, _)| (name.text.clone(), chip.pads[chip.decls[&name.text].index]))
        .collect();
    let resources = resource_map(
        &chip.decls,
        &chip.pads,
        &chip.edges,
        items,
        &streams,
        &chip.protocol_action_lines,
        protocol_controller.is_some(),
    );
    let kinds = [
        Kind::Pin,
        Kind::Timer,
        Kind::Shifter,
        Kind::Counter,
        Kind::Crc,
        Kind::Reg,
        Kind::Event,
        Kind::Flag,
        Kind::Protocol,
    ];
    let mut usage: Vec<_> = kinds
        .iter()
        .map(|&kind| {
            let (capacity, plural) = kind.capacity();
            (
                plural.split(" (").next().unwrap_or(plural),
                if kind == Kind::Protocol && protocol_controller.is_some() {
                    0
                } else {
                    counts[kind as usize]
                },
                capacity,
            )
        })
        .collect();
    if byte_usage != 0 {
        usage.push(("byte tiles", byte_usage, hardware::BYTE_TILES));
    }
    usage.push(("edge cells", chip.edges.len(), hardware::EDGE_CELLS));
    usage.push(("memory", image.len(), hardware::SRAM_BYTES));
    Ok(Design {
        clock_hz,
        env: chip.env,
        image,
        pins,
        streams,
        exports,
        usage,
        resources,
        protocol_mappings,
        protocol_placement_search: actor_search.report,
        tests,
        invariants,
    })
}

fn protocol_graph(
    name: &Name,
    input: &Expr,
    source: &[ProtocolTransition],
    chip: &Chip<'_>,
) -> Result<crate::protocol::ProtocolGraph> {
    let mut transitions = Vec::with_capacity(source.len());
    let mut states = 1usize;
    for transition in source {
        let state = |expr: &Expr| -> Result<usize> {
            let value = chip.env.integer(expr)?;
            usize::try_from(value)
                .ok()
                .filter(|&value| value < crate::protocol::MAX_LOGICAL_STATES)
                .ok_or_else(|| {
                    Error::new(
                        expr.span,
                        format!(
                            "protocol state must be in 0..{}",
                            crate::protocol::MAX_LOGICAL_STATES - 1
                        ),
                    )
                })
        };
        let from = state(&transition.from)?;
        let to = state(&transition.to)?;
        states = states.max(from + 1).max(to + 1);
        let action = transition
            .action
            .as_ref()
            .map(|expr| {
                let value = chip.env.integer(expr)?;
                usize::try_from(value)
                    .ok()
                    .filter(|&value| (1..=crate::protocol::ACTOR_ACTIONS).contains(&value))
                    .ok_or_else(|| {
                        Error::new(
                            expr.span,
                            format!(
                                "protocol action must be in 1..={}",
                                crate::protocol::ACTOR_ACTIONS
                            ),
                        )
                    })
            })
            .transpose()?;
        transitions.push(crate::protocol::Transition {
            from,
            input_high: transition.input_high,
            to,
            action,
        });
    }
    Ok(crate::protocol::ProtocolGraph {
        name: name.text.clone(),
        input_line: chip.signal(input)?.0,
        states,
        initial_state: 0,
        transitions,
    })
}

/// Maximum emitted bytes for one PX8 protocol transition: WEV plus its operand
/// prefix, an optional FIRE, and the fixed three-byte jump.
fn controller_step_bytes(graph: &crate::protocol::ProtocolGraph) -> usize {
    let wait_bytes = if graph.input_line < 16 { 1 } else { 2 };
    (0..graph.states)
        .map(|state| {
            let action_bytes = graph
                .transitions
                .iter()
                .find(|transition| transition.from == state && transition.input_high)
                .and_then(|transition| transition.action)
                .map_or(0, |_| 1);
            wait_bytes + action_bytes + 3
        })
        .max()
        .unwrap_or(wait_bytes + 3)
}

/// Keep the compiler's allocation report tied to the same encoding helpers
/// used to emit RTL configuration and signal selectors.
fn resource_map(
    decls: &HashMap<String, Decl>,
    pads: &[usize],
    edges: &HashMap<(usize, bool), usize>,
    items: &[Item],
    streams: &BTreeMap<String, Stream>,
    protocol_action_lines: &HashMap<String, [Option<Line>; 3]>,
    protocol_controller: bool,
) -> Vec<ResourceAssignment> {
    let mut resources = Vec::new();
    for (name, decl) in decls {
        let k = decl.index;
        let (kind, index, config, outputs) = match decl.kind {
            Kind::Pin => {
                let pad = pads[k];
                (
                    "pin cell",
                    pad,
                    Some(config::pin(pad)),
                    vec![("level", Line::pin_level(pad).0)],
                )
            }
            Kind::Timer => (
                "timer",
                k,
                Some(config::timer(k, 0)),
                vec![
                    ("tick", Line::timer_rise(k).0),
                    ("fall", Line::timer_fall(k).0),
                    ("out", Line::timer_out(k).0),
                ],
            ),
            Kind::Shifter => (
                "shifter",
                k,
                Some(config::shifter(k, 0)),
                vec![
                    ("word", Line::shifter_word(k).0),
                    ("done", Line::shifter_stop(k).0),
                    ("out", Line::shifter_out(k).0),
                    ("busy", Line::shifter_active(k).0),
                ],
            ),
            Kind::Counter => (
                "counter",
                k,
                Some(config::counter(k, 0)),
                vec![
                    ("reached", Line::counter_reached(k).0),
                    ("at_least", Line::counter_at_least(k).0),
                ],
            ),
            Kind::Reg => (
                "logic cell",
                k,
                Some(config::logic(k, 0)),
                vec![("value", Line::logic(k).0)],
            ),
            Kind::Crc => (
                "CRC",
                k,
                Some(config::crc(k, 0)),
                vec![("out", Line::crc_out(k).0), ("ok", Line::crc_ok(k).0)],
            ),
            Kind::Event => ("event", k, None, vec![("fire", Line::fire(k).0)]),
            Kind::Flag => ("flag", k, None, vec![("value", Line::flag(k).0)]),
            Kind::Protocol => {
                let actions = protocol_action_lines
                    .get(name)
                    .copied()
                    .unwrap_or([None; 3]);
                let mut outputs = Vec::new();
                for (i, action) in actions.iter().enumerate() {
                    if let Some(line) = action {
                        outputs.push((["action1", "action2", "action3"][i], line.0));
                    }
                }
                if protocol_controller {
                    ("PX8 protocol controller", k, None, outputs)
                } else {
                    ("protocol actor", k, Some(0x90 + (k * 8) as u8), outputs)
                }
            }
        };
        resources.push(ResourceAssignment {
            name: name.clone(),
            kind,
            index,
            config,
            outputs,
        });
    }
    for (&(pad, falling), &index) in edges {
        let edge_name = if falling { "fall" } else { "rise" };
        resources.push(ResourceAssignment {
            name: format!("uio{pad}.{edge_name}"),
            kind: "edge cell",
            index,
            config: Some(config::edge(index)),
            outputs: vec![(edge_name, Line::edge(index).0)],
        });
    }
    for item in items {
        let Item::Stream { name, stages, .. } = item else {
            continue;
        };
        let stream = &streams[&name.text];
        let receive = stream.direction == Direction::FromChip;
        for (stage, declaration) in stages.iter().enumerate() {
            let index = 4 * stream.channel + 2 * usize::from(receive) + stage;
            let kind = match declaration.kind.text.as_str() {
                "pass" => "byte pass",
                "escape" => "byte escape",
                "unescape" => "byte unescape",
                "checksum" => "byte checksum",
                "tag" => "byte tag",
                "filter" => "byte filter",
                _ => "byte stage",
            };
            resources.push(ResourceAssignment {
                name: format!("{}.stage{}", name.text, stage),
                kind,
                index,
                config: Some(config::byte_tile(index, 0)),
                outputs: Vec::new(),
            });
        }
    }
    resources.sort_by(|a, b| (a.kind, a.index, &a.name).cmp(&(b.kind, b.index, &b.name)));
    resources
}

fn clock(expr: &Expr) -> Result<i128> {
    let probe = Env {
        clock_hz: 1,
        ..Env::default()
    };
    match probe.eval(expr)? {
        Value::Hertz(hz) => hz.whole().filter(|&hz| hz > 0).ok_or_else(|| {
            Error::new(
                expr.span,
                "the clock must be a whole, positive number of Hz",
            )
        }),
        _ => Err(Error::new(
            expr.span,
            "expected a frequency, such as `clock 50 MHz`",
        )),
    }
}

fn collect_expr_edges<'a>(expr: &'a Expr, uses: &mut Vec<(&'a str, bool, Span)>) {
    match &expr.kind {
        ExprKind::Field(object, field) => {
            if (field.text == "rise" || field.text == "fall")
                && let ExprKind::Name(name) = &object.kind
            {
                uses.push((name, field.text == "fall", field.span));
            }
            collect_expr_edges(object, uses);
        }
        ExprKind::Call(callee, args) => {
            collect_expr_edges(callee, uses);
            for arg in args {
                collect_expr_edges(&arg.value, uses);
            }
        }
        ExprKind::List(values) => {
            for value in values {
                collect_expr_edges(value, uses);
            }
        }
        ExprKind::Unary(_, value) => collect_expr_edges(value, uses),
        ExprKind::Binary(_, a, b) => {
            collect_expr_edges(a, uses);
            collect_expr_edges(b, uses);
        }
        ExprKind::If(a, b, c) => {
            collect_expr_edges(a, uses);
            collect_expr_edges(b, uses);
            collect_expr_edges(c, uses);
        }
        _ => {}
    }
}

fn collect_stmt_edges<'a>(stmt: &'a Stmt, uses: &mut Vec<(&'a str, bool, Span)>) {
    match &stmt.kind {
        StmtKind::Wait { signal, .. } | StmtKind::Reply(signal) | StmtKind::Expr(signal) => {
            collect_expr_edges(signal, uses)
        }
        StmtKind::Repeat { count, body } => {
            collect_expr_edges(count, uses);
            for stmt in body {
                collect_stmt_edges(stmt, uses);
            }
        }
        StmtKind::For { iter, body, .. } => {
            collect_expr_edges(iter, uses);
            for stmt in body {
                collect_stmt_edges(stmt, uses);
            }
        }
        StmtKind::If {
            condition,
            then,
            otherwise,
        } => {
            collect_expr_edges(condition, uses);
            for stmt in then.iter().chain(otherwise) {
                collect_stmt_edges(stmt, uses);
            }
        }
        StmtKind::Expect { condition, .. } => collect_expr_edges(condition, uses),
        StmtKind::Attach { peer, .. } => collect_expr_edges(peer, uses),
        StmtKind::Wire { from, to }
        | StmtKind::Assign {
            target: from,
            value: to,
        } => {
            collect_expr_edges(from, uses);
            collect_expr_edges(to, uses);
        }
        StmtKind::Set { target, fields } => {
            collect_expr_edges(target, uses);
            for field in fields {
                if let Some(value) = &field.value {
                    collect_expr_edges(value, uses);
                }
            }
        }
        StmtKind::Fire(_) | StmtKind::Fail(_) => {}
    }
}

fn collect_item_edges<'a>(item: &'a Item, uses: &mut Vec<(&'a str, bool, Span)>) {
    match item {
        Item::Contexts(expr) | Item::Clock(expr) | Item::Const { value: expr, .. } => {
            collect_expr_edges(expr, uses)
        }
        Item::Context { index, body } => {
            collect_expr_edges(index, uses);
            for item in body {
                collect_item_edges(item, uses);
            }
        }
        Item::Pin { source, .. } => {
            if let Some(expr) = source {
                collect_expr_edges(expr, uses);
            }
        }
        Item::Tile { fields, .. } => {
            for field in fields {
                if let Some(expr) = &field.value {
                    collect_expr_edges(expr, uses);
                }
            }
        }
        Item::Reg {
            init, on, value, ..
        } => {
            for expr in init.iter().chain(on).chain(std::iter::once(value)) {
                collect_expr_edges(expr, uses);
            }
        }
        Item::Stream { stages, .. } => {
            for stage in stages {
                for field in &stage.fields {
                    if let Some(expr) = &field.value {
                        collect_expr_edges(expr, uses);
                    }
                }
            }
        }
        Item::Fn { body, .. } => {
            for stmt in body {
                collect_stmt_edges(stmt, uses);
            }
        }
        Item::Protocol { input, .. } => collect_expr_edges(input, uses),
        Item::Event(_) | Item::Flag(_) | Item::Test { .. } | Item::Always { .. } => {}
    }
}

/// Give every pin a distinct pad from its allowed list (bipartite matching
/// by augmenting paths), or explain why no assignment exists.
fn assign_pads(pins: &[(&Name, Vec<usize>)]) -> Result<Vec<usize>> {
    fn augment(
        pin: usize,
        pins: &[(&Name, Vec<usize>)],
        owner: &mut [Option<usize>],
        seen: &mut [bool],
    ) -> bool {
        // Prefer a free pad, so pins keep the lowest pads in declaration order.
        if let Some(&pad) = pins[pin].1.iter().find(|&&pad| owner[pad].is_none()) {
            owner[pad] = Some(pin);
            return true;
        }
        for &pad in &pins[pin].1 {
            if !seen[pad] {
                seen[pad] = true;
                if owner[pad].is_none_or(|other| augment(other, pins, owner, seen)) {
                    owner[pad] = Some(pin);
                    return true;
                }
            }
        }
        false
    }
    let mut owner = vec![None; hardware::PADS];
    for (pin, (name, _)) in pins.iter().enumerate() {
        if !augment(pin, pins, &mut owner, &mut [false; hardware::PADS]) {
            return Err(Error::new(
                name.span,
                format!(
                    "no free pad left for `{}`: the pins before it already take every pad it allows",
                    name.text
                ),
            ));
        }
    }
    let mut pads = vec![0; pins.len()];
    for (pad, pin) in owner.iter().enumerate() {
        if let Some(pin) = pin {
            pads[*pin] = pad;
        }
    }
    Ok(pads)
}

/// Index fields by name, rejecting unknown and repeated ones. Later fields
/// override earlier ones, which is how `.set { ... }` overrides a declaration.
pub(crate) fn fields<'f>(
    list: &[&'f Field],
    allowed: &[&str],
    noun: &str,
) -> Result<HashMap<&'f str, &'f Field>> {
    let mut found = HashMap::new();
    for f in list {
        if !allowed.contains(&f.name.text.as_str()) {
            return Err(Error::new(
                f.name.span,
                format!(
                    "a {noun} has no `{}`; it has: {}",
                    f.name.text,
                    allowed.join(", ")
                ),
            ));
        }
        found.insert(f.name.text.as_str(), *f);
    }
    Ok(found)
}

pub(crate) fn value(f: &Field) -> Result<&Expr> {
    f.value.as_ref().ok_or_else(|| {
        Error::new(
            f.name.span,
            format!("`{}` needs a value: `{}: ...`", f.name.text, f.name.text),
        )
    })
}

impl Chip<'_> {
    pub(crate) fn decl(&self, expr: &Expr) -> Result<(String, Decl)> {
        let ExprKind::Name(name) = &expr.kind else {
            return Err(Error::new(expr.span, "expected a name"));
        };
        let decl = *self
            .decls
            .get(name)
            .ok_or_else(|| Error::new(expr.span, format!("`{name}` is not declared")))?;
        Ok((name.clone(), decl))
    }

    /// Resolve a signal reference to its line.
    pub(crate) fn signal(&self, expr: &Expr) -> Result<Line> {
        match &expr.kind {
            ExprKind::Int(0) => Ok(Line::ZERO),
            ExprKind::Int(1) => Ok(Line::ONE),
            ExprKind::Name(_) => {
                let (name, decl) = self.decl(expr)?;
                match decl.kind {
                    Kind::Pin => Ok(Line::pin_level(self.pads[decl.index])),
                    Kind::Reg => Ok(Line::logic(decl.index)),
                    Kind::Event => Ok(Line::fire(decl.index)),
                    Kind::Flag => Ok(Line::flag(decl.index)),
                    Kind::Protocol => Err(Error::new(
                        expr.span,
                        "a protocol actor has action pulses; use `.action1`, `.action2` or `.action3`",
                    )),
                    kind => Err(Error::new(
                        expr.span,
                        format!(
                            "`{name}` is a {}; use one of its signals: {}",
                            kind.noun(),
                            kind.outputs()
                        ),
                    )),
                }
            }
            ExprKind::Field(object, field) => {
                let (name, decl) = self.decl(object)?;
                let k = decl.index;
                if decl.kind == Kind::Protocol {
                    let index = match field.text.as_str() {
                        "action1" => 0,
                        "action2" => 1,
                        "action3" => 2,
                        _ => {
                            return Err(Error::new(
                                field.span,
                                format!(
                                    "a protocol actor has no signal `{}`; it has `.action1`, `.action2` and `.action3`",
                                    field.text
                                ),
                            ));
                        }
                    };
                    return self.protocol_action_lines.get(&name)
                        .and_then(|lines| lines[index])
                        .ok_or_else(|| Error::new(field.span, format!("protocol `{name}` has no `{}` output in its selected implementation", field.text)));
                }
                Ok(match (decl.kind, field.text.as_str()) {
                    (Kind::Pin, "rise" | "fall") => {
                        let pad = self.pads[k];
                        let falling = field.text == "fall";
                        Line::edge(self.edges[&(pad, falling)])
                    }
                    (Kind::Timer, "tick" | "rise") => Line::timer_rise(k),
                    (Kind::Timer, "fall") => Line::timer_fall(k),
                    (Kind::Timer, "out") => Line::timer_out(k),
                    (Kind::Shifter, "word") => Line::shifter_word(k),
                    (Kind::Shifter, "done") => Line::shifter_stop(k),
                    (Kind::Shifter, "out") => Line::shifter_out(k),
                    (Kind::Shifter, "busy") => Line::shifter_active(k),
                    (Kind::Counter, "reached") => Line::counter_reached(k),
                    (Kind::Counter, "at_least") => Line::counter_at_least(k),
                    (Kind::Crc, "out") => Line::crc_out(k),
                    (Kind::Crc, "ok") => Line::crc_ok(k),
                    (kind, _) => {
                        return Err(Error::new(
                            field.span,
                            format!(
                                "a {} has no signal `{}`; it has {}",
                                kind.noun(),
                                field.text,
                                kind.outputs()
                            ),
                        ));
                    }
                })
            }
            _ => Err(Error::new(expr.span, "expected a signal")),
        }
    }

    fn level(&self, expr: &Expr) -> Result<Line> {
        let line = self.signal(expr)?;
        if !line.is_level() {
            return Err(Error::new(
                expr.span,
                "this needs a level (a value that stays high or low), but this is a one-cycle pulse or a constant",
            ));
        }
        Ok(line)
    }

    fn pulse(&self, expr: &Expr) -> Result<Line> {
        let line = self.signal(expr)?;
        if line.is_level() {
            return Err(Error::new(
                expr.span,
                "this needs a pulse (such as `.tick` or `.rise`), but this is a level",
            ));
        }
        Ok(line)
    }

    pub(crate) fn bit(&self, expr: &Expr) -> Result<u16> {
        match self.env.integer(expr)? {
            b @ (0 | 1) => Ok(b as u16),
            _ => Err(Error::new(expr.span, "expected 0 or 1")),
        }
    }

    fn ranged(expr: &Expr, value: i128, low: i128, high: i128, what: &str) -> Result<u16> {
        if (low..=high).contains(&value) {
            Ok(value as u16)
        } else {
            Err(Error::new(
                expr.span,
                format!("{what} is {value}; it must be between {low} and {high}"),
            ))
        }
    }

    fn pad(&self, expr: &Expr) -> Result<usize> {
        let pad = self.env.integer(expr)?;
        usize::try_from(pad)
            .ok()
            .filter(|&p| p < hardware::PADS)
            .ok_or_else(|| {
                Error::new(
                    expr.span,
                    format!("pads are numbered 0 to {}", hardware::PADS - 1),
                )
            })
    }

    pub(crate) fn pin(
        &self,
        name: &Name,
        mode: &Name,
        source: Option<&Expr>,
    ) -> Result<Vec<(u8, u16)>> {
        let pad = self.pads[self.decls[&name.text].index];
        let mode = match mode.text.as_str() {
            "in" => PinMode::Input,
            "out" => PinMode::PushPull,
            "open_drain" => PinMode::OpenDrain,
            other => {
                return Err(Error::new(
                    mode.span,
                    format!("pin modes are `in`, `out` and `open_drain`, not `{other}`"),
                ));
            }
        };
        let source = match (mode, source) {
            (PinMode::Input, None) => return Ok(Vec::new()),
            (PinMode::Input, Some(source)) => {
                return Err(Error::new(
                    source.span,
                    "an `in` pin only listens; remove `= ...` or make it `out`",
                ));
            }
            (_, None) => {
                return Err(Error::new(
                    name.span,
                    "an output pin needs a source: `= some.signal`",
                ));
            }
            (_, Some(source)) => source,
        };
        if matches!(
            source.kind,
            ExprKind::Unary(..) | ExprKind::Binary(..) | ExprKind::If(..)
        ) {
            return Err(Error::new(
                source.span,
                "a pin copies one signal; compute this in a `reg` (adds one cycle)",
            ));
        }
        let encoded = match self.signal(source)? {
            // Constants have their own encoding on pins.
            line if line.is_constant() => u16::from(line.0) << 4 | 1 << 5,
            line if line.is_level() => line.level_select(),
            _ => {
                return Err(Error::new(
                    source.span,
                    "a pin is driven by a level, but this is a one-cycle pulse; drive it from a `reg` or a `.out`/`.busy` signal",
                ));
            }
        };
        Ok(vec![(config::pin(pad), encoded | (mode as u16) << 6)])
    }

    /// Configuration writes for a timer, shifter or counter from its fields.
    pub(crate) fn tile(&self, name: &Name, list: &[&Field]) -> Result<Vec<(u8, u16)>> {
        let decl = self.decls[&name.text];
        match decl.kind {
            Kind::Timer => self.timer(name, decl.index, list),
            Kind::Shifter => self.shifter(name, decl.index, list),
            Kind::Counter => self.counter(name, decl.index, list),
            Kind::Crc => self.crc(name, decl.index, list),
            kind => Err(Error::new(
                name.span,
                format!("a {} has no settings to change", kind.noun()),
            )),
        }
    }

    fn timer(&self, name: &Name, k: usize, list: &[&Field]) -> Result<Vec<(u8, u16)>> {
        let f = fields(
            list,
            &["every", "first", "pulse", "idle", "run", "hold", "resync"],
            "timer",
        )?;
        let every = f
            .get("every")
            .ok_or_else(|| Error::new(name.span, "a timer needs `every: <duration>`"))?;
        let every = value(every)?;
        let reload = Self::ranged(
            every,
            self.env.cycles(
                every,
                &format!("timer `{}` would tick every {{}}", name.text),
                true,
            )? - 1,
            0,
            hardware::MAX_TIMER_CYCLES - 1,
            "`every` minus one cycle",
        )?;
        let phase = match f.get("first") {
            Some(first) => {
                let first = value(first)?;
                Self::ranged(
                    first,
                    self.env.cycles(
                        first,
                        &format!("timer `{}`'s first tick would come after {{}}", name.text),
                        false,
                    )? - 1,
                    0,
                    hardware::MAX_TIMER_CYCLES - 1,
                    "`first` minus one cycle",
                )?
            }
            None => reload,
        };
        if let Some(pulse) = f.get("pulse").filter(|p| p.value.is_some()) {
            return Err(Error::new(
                pulse.name.span,
                "`pulse` is a flag; write just `pulse`",
            ));
        }
        let pulse = u16::from(f.contains_key("pulse"));
        let idle = f
            .get("idle")
            .map(|i| value(i).and_then(|e| self.bit(e)))
            .transpose()?
            .unwrap_or(0);
        let run = f.get("run").ok_or_else(|| {
            Error::new(
                name.span,
                "a timer needs `run: <start> until <stop>`, or `run: 1` to run forever",
            )
        })?;
        let (start, stop) = match &value(run)?.kind {
            ExprKind::Binary("until", start, stop) => (self.signal(start)?, self.signal(stop)?),
            _ => (self.signal(value(run)?)?, Line::ZERO),
        };
        let mut sync = 0;
        if let Some(resync) = f.get("resync") {
            sync |= u16::from(self.signal(value(resync)?)?.0) | 1 << 6;
        }
        if let Some(hold) = f.get("hold") {
            let expr = value(hold)?;
            let (line, invert) = match &expr.kind {
                ExprKind::Unary("!", inner) => (self.level(inner)?, 1),
                _ => (self.level(expr)?, 0),
            };
            sync |= line.level_select() << 7 | 1 << 12 | invert << 13;
        }
        Ok(vec![
            (config::timer(k, 0), reload),
            (config::timer(k, 1), phase),
            (config::timer(k, 3), sync),
            (
                config::timer(k, 2),
                u16::from(start.0) | u16::from(stop.0) << 6 | idle << 12 | pulse << 13,
            ),
        ])
    }

    /// `frame: [0, data(8, msb_first), even, 1]`: framing bits, data bits, optional parity, framing bits.
    fn frame(&self, expr: &Expr) -> Result<(u16, i128)> {
        let ExprKind::List(parts) = &expr.kind else {
            return Err(Error::new(
                expr.span,
                "a frame is a list such as `[0, data(8), 1]`",
            ));
        };
        let is_data = |p: &Expr| matches!(&p.kind, ExprKind::Call(f, _) if matches!(&f.kind, ExprKind::Name(n) if n == "data"));
        let data_at = parts
            .iter()
            .position(is_data)
            .ok_or_else(|| Error::new(expr.span, "a frame needs exactly one `data(bits)`"))?;
        let ExprKind::Call(_, args) = &parts[data_at].kind else {
            unreachable!()
        };
        let count = args.first().ok_or_else(|| {
            Error::new(parts[data_at].span, "`data` needs a bit count: `data(8)`")
        })?;
        let bits = self.env.integer(&count.value)?;
        Self::ranged(
            &count.value,
            bits,
            1,
            hardware::MAX_WORD_BITS,
            "the word length",
        )?;
        let mut msb = 0;
        for arg in &args[1..] {
            match &arg.value.kind {
                ExprKind::Name(n) if n == "msb_first" => msb = 1,
                ExprKind::Name(n) if n == "lsb_first" => msb = 0,
                _ => {
                    return Err(Error::new(
                        arg.value.span,
                        "expected `msb_first` or `lsb_first`",
                    ));
                }
            }
        }
        let mut after = &parts[data_at + 1..];
        let mut parity = 0;
        if let Some(Expr {
            kind: ExprKind::Name(n),
            span,
        }) = after.first()
            && !self.env.consts.contains_key(n)
        {
            parity = match n.as_str() {
                "even" => 1,
                "odd" => 2,
                _ => return Err(Error::new(*span, "expected `even`, `odd`, 0 or 1")),
            };
            after = &after[1..];
        }
        let framing = |bits: &[Expr], side: &str| -> Result<(u16, u16)> {
            let values = bits
                .iter()
                .map(|b| self.bit(b))
                .collect::<Result<Vec<_>>>()?;
            if values.len() as i128 > hardware::MAX_FRAME_BITS {
                return Err(Error::new(
                    expr.span,
                    format!(
                        "at most {} framing bits {side} the data",
                        hardware::MAX_FRAME_BITS
                    ),
                ));
            }
            if values.windows(2).any(|w| w[0] != w[1]) {
                return Err(Error::new(
                    expr.span,
                    format!("framing bits {side} the data must all be equal"),
                ));
            }
            Ok((values.len() as u16, values.first().copied().unwrap_or(0)))
        };
        let (pre, pre_value) = framing(&parts[..data_at], "before")?;
        let (post, post_value) = framing(after, "after")?;
        Ok((
            (bits - 1) as u16
                | pre << 4
                | pre_value << 6
                | post << 7
                | post_value << 9
                | parity << 10
                | msb << 12,
            bits,
        ))
    }

    fn shifter(&self, name: &Name, k: usize, list: &[&Field]) -> Result<Vec<(u8, u16)>> {
        let f = fields(
            list,
            &[
                "frame",
                "idle",
                "start",
                "shift",
                "sample",
                "lead",
                "end_on_sample",
                "burst",
                "end_on_packet",
                "end",
                "abort",
                "stuff_after",
                "stuff_ones",
                "nrzi",
                "code_received",
                "arm_at_zero",
                "invert_crc_tail",
                "bit_strobe",
                "crc_tail_bits",
                "manchester",
                "stuff_while",
                "crc_skip_bits",
            ],
            "shifter",
        )?;
        let (mut frame, bits) = match f.get("frame") {
            Some(frame) => self.frame(value(frame)?)?,
            None => (7, 8),
        };
        let Fifo { pull, push } = self.fifos[k];
        if (pull || push) && bits > hardware::FIFO_WORD_BITS {
            return Err(Error::new(
                name.span,
                format!(
                    "`{}` exchanges words with the host, so its data is at most 8 bits",
                    name.text
                ),
            ));
        }
        for (flag, bit) in [("lead", 13), ("end_on_sample", 14)] {
            if f.contains_key(flag) {
                frame |= 1 << bit;
            }
        }
        if let Some(idle) = f.get("idle") {
            frame |= self.bit(value(idle)?)? << 15;
        }
        let shift = f
            .get("shift")
            .map(|s| self.signal(value(s)?))
            .transpose()?
            .unwrap_or(Line::ZERO);
        let (sample, input) = match f.get("sample") {
            None => (Line::ZERO, None),
            Some(s) => match &value(s)?.kind {
                ExprKind::Binary("at", input, when) => {
                    (self.signal(when)?, Some(self.level(input)?))
                }
                _ => {
                    return Err(Error::new(
                        value(s)?.span,
                        "say which wire to read and when: `sample: rx at bit.tick`",
                    ));
                }
            },
        };
        if push && input.is_none() {
            return Err(Error::new(
                name.span,
                format!(
                    "`{}` sends received words to the host, so it needs `sample: <pin> at <signal>`",
                    name.text
                ),
            ));
        }
        let start = f
            .get("start")
            .map(|s| self.signal(value(s)?))
            .transpose()?
            .unwrap_or(Line::ZERO);
        let input = input.map_or(0, Line::level_select);
        let mut writes = vec![
            (config::shifter(k, 1), frame),
            (
                config::shifter(k, 2),
                u16::from(shift.0) | u16::from(sample.0) << 6,
            ),
            (
                config::shifter(k, 3),
                u16::from(start.0) | input << 6 | u16::from(pull) << 11 | u16::from(push) << 12,
            ),
        ];
        if ["burst", "end_on_packet", "end", "abort"]
            .iter()
            .any(|key| f.contains_key(key))
        {
            let enabled = |key: &str| -> Result<u16> {
                match f.get(key) {
                    None => Ok(0),
                    Some(field) => match &field.value {
                        None => Ok(1),
                        Some(expr) => self.bit(expr),
                    },
                }
            };
            let burst = enabled("burst")?;
            let packet = enabled("end_on_packet")?;
            if burst != 0 && !f.contains_key("end") {
                return Err(Error::new(
                    name.span,
                    "a burst needs an explicit `end` level; use a zero-valued reg for packet-tag-only endings",
                ));
            }
            if (packet != 0 || f.contains_key("end")) && burst == 0 {
                return Err(Error::new(
                    name.span,
                    "`end` and `end_on_packet` need `burst`",
                ));
            }
            if packet != 0 && !pull {
                return Err(Error::new(
                    name.span,
                    "`end_on_packet` needs a host-to-shifter FIFO connection",
                ));
            }
            let end = f
                .get("end")
                .map(|field| self.level(value(field)?))
                .transpose()?;
            if end.is_some_and(|line| !line.is_level()) {
                return Err(Error::new(
                    name.span,
                    "`end` needs a level line; use a pin, flag or reg",
                ));
            }
            let abort = f
                .get("abort")
                .map(|field| self.signal(value(field)?))
                .transpose()?
                .unwrap_or(Line::ZERO);
            writes.push((
                config::shifter_burst(k),
                end.map_or(0, Line::level_select)
                    | burst << 5
                    | packet << 6
                    | u16::from(abort.0) << 7,
            ));
        }
        let coder_fields = [
            "stuff_after",
            "stuff_ones",
            "nrzi",
            "code_received",
            "arm_at_zero",
            "invert_crc_tail",
            "bit_strobe",
            "crc_tail_bits",
            "manchester",
            "stuff_while",
            "crc_skip_bits",
        ];
        if coder_fields.iter().any(|key| f.contains_key(key)) {
            let flag = |key: &str| -> Result<u16> {
                match f.get(key) {
                    None => Ok(0),
                    Some(field) => field
                        .value
                        .as_ref()
                        .map(|e| self.bit(e))
                        .transpose()
                        .map(|bit| bit.unwrap_or(1)),
                }
            };
            let number = |key: &str, high: i128| -> Result<u16> {
                match f.get(key) {
                    None => Ok(0),
                    Some(field) => {
                        let expr = value(field)?;
                        Self::ranged(expr, self.env.integer(expr)?, 0, high, key)
                    }
                }
            };
            let nrzi = flag("nrzi")?;
            let manchester = flag("manchester")?;
            let received = flag("code_received")?;
            if manchester != 0 && (nrzi != 0 || received != 0) {
                return Err(Error::new(
                    name.span,
                    "Manchester transmit cannot be combined with NRZI or receive coding",
                ));
            }
            let coder = number("stuff_after", 7)?
                | flag("stuff_ones")? << 3
                | nrzi << 4
                | received << 5
                | flag("arm_at_zero")? << 6
                | flag("invert_crc_tail")? << 7
                | flag("bit_strobe")? << 8
                | number("crc_tail_bits", 63)? << 9
                | manchester << 15;
            let gate = f
                .get("stuff_while")
                .map(|field| self.level(value(field)?))
                .transpose()?;
            let coder_gate = gate.map_or(0, Line::level_select)
                | u16::from(gate.is_some()) << 5
                | number("crc_skip_bits", 127)? << 6;
            writes.push((config::shifter_coder(k), coder));
            writes.push((config::shifter_coder_gate(k), coder_gate));
        }
        Ok(writes)
    }

    fn counter(&self, name: &Name, k: usize, list: &[&Field]) -> Result<Vec<(u8, u16)>> {
        let f = fields(list, &["count", "streak", "limit", "clear"], "counter")?;
        let step = match f.get("count") {
            Some(c) => self.signal(value(c)?)?,
            None => return Err(Error::new(name.span, "a counter needs `count: <signal>`")),
        };
        // `streak: ones(x)` counts the current run of equal bits instead of every step.
        let (mode, data) = match f.get("streak") {
            None => (0, 0),
            Some(streak) => {
                let expr = value(streak)?;
                let wrong = || {
                    Error::new(
                        expr.span,
                        "expected `ones(signal)`, `zeros(signal)` or `equal(signal)`",
                    )
                };
                let ExprKind::Call(kind, args) = &expr.kind else {
                    return Err(wrong());
                };
                let (ExprKind::Name(kind), [arg]) = (&kind.kind, args.as_slice()) else {
                    return Err(wrong());
                };
                let mode = match kind.as_str() {
                    "ones" => 1,
                    "zeros" => 2,
                    "equal" => 3,
                    _ => return Err(wrong()),
                };
                (mode, self.level(&arg.value)?.level_select())
            }
        };
        let limit = match f.get("limit") {
            Some(l) => {
                let expr = value(l)?;
                Self::ranged(expr, self.env.integer(expr)?, 0, 255, "the limit")?
            }
            None => 0,
        };
        let clear = f
            .get("clear")
            .map(|c| self.pulse(value(c)?))
            .transpose()?
            .unwrap_or(Line::ZERO);
        Ok(vec![
            (config::counter(k, 0), limit),
            (config::counter(k, 2), u16::from(clear.0)),
            (
                config::counter(k, 1),
                u16::from(step.0) | data << 6 | mode << 11,
            ),
        ])
    }

    /// A CRC register fed bit by bit: `absorb` steps it with `data`, `emit`
    /// shifts its complemented check bits out on `.out`, and `.ok` compares
    /// it with `check`. `restart` reloads it; `start: zeros` starts it at 0.
    fn crc(&self, name: &Name, k: usize, list: &[&Field]) -> Result<Vec<(u8, u16)>> {
        let f = fields(
            list,
            &[
                "width",
                "poly",
                "check",
                "absorb",
                "data",
                "emit",
                "restart",
                "start",
                "absorb_while",
                "emit_while",
                "link",
            ],
            "crc",
        )?;
        let number = |field: &str, high: i128| -> Result<i128> {
            let f = f
                .get(field)
                .ok_or_else(|| Error::new(name.span, format!("a crc needs `{field}`")))?;
            let expr = value(f)?;
            let n = self.env.integer(expr)?;
            if !(0..=high).contains(&n) {
                return Err(Error::new(
                    expr.span,
                    format!("`{field}` is {n}; it must be between 0 and {high}"),
                ));
            }
            Ok(n)
        };
        let width = number("width", hardware::MAX_CRC_BITS)?;
        if width == 0 {
            return Err(Error::new(name.span, "a crc is at least 1 bit wide"));
        }
        let poly = number("poly", (1i128 << width) - 1)?;
        let check = number("check", (1i128 << width) - 1)?;
        let line = |field: &str| -> Result<Line> {
            f.get(field)
                .map(|e| self.signal(value(e)?))
                .transpose()
                .map(|l| l.unwrap_or(Line::ZERO))
        };
        let absorb = line("absorb")?;
        let emit = line("emit")?;
        let link = f
            .get("link")
            .map(|field| self.shifter_index(value(field)?))
            .transpose()?;
        if link.is_some()
            && ["absorb", "data", "emit"]
                .iter()
                .any(|key| f.contains_key(key))
        {
            return Err(Error::new(
                name.span,
                "a linked CRC takes bits from its shifter; omit `absorb`, `data` and `emit`",
            ));
        }
        let restart = f
            .get("restart")
            .map(|e| self.pulse(value(e)?))
            .transpose()?
            .unwrap_or(Line::ZERO);
        let data = match f.get("data") {
            Some(d) => self.level(value(d)?)?.level_select(),
            None => 0,
        };
        let ones = match f
            .get("start")
            .map(|s| value(s))
            .transpose()?
            .map(|e| &e.kind)
        {
            None => 1,
            Some(ExprKind::Name(n)) if n == "ones" => 1,
            Some(ExprKind::Name(n)) if n == "zeros" => 0,
            Some(_) => return Err(Error::new(name.span, "`start` is `ones` or `zeros`")),
        };
        let mut gates = 0;
        if let Some(g) = f.get("absorb_while") {
            gates |= self.level(value(g)?)?.level_select() | 1 << 5;
        }
        if let Some(g) = f.get("emit_while") {
            gates |= self.level(value(g)?)?.level_select() << 6 | 1 << 11;
        }
        Ok(vec![
            (config::crc(k, 0), poly as u16),
            (config::crc(k, 1), (poly >> 16) as u16),
            (config::crc(k, 2), check as u16),
            (config::crc(k, 3), (check >> 16) as u16),
            (
                config::crc(k, 6),
                (width - 1) as u16
                    | ones << 5
                    | u16::from(link.is_some()) << 6
                    | (link.unwrap_or(0) as u16) << 7,
            ),
            (
                config::crc(k, 5),
                u16::from(emit.0) | u16::from(restart.0) << 6,
            ),
            (config::crc(k, 4), u16::from(absorb.0) | data << 6),
            (config::crc(k, 7), gates),
        ])
    }

    fn reg(
        &self,
        name: &Name,
        init: Option<&Expr>,
        on: Option<&Expr>,
        expr: &Expr,
    ) -> Result<Vec<(u8, u16)>> {
        let k = self.decls[&name.text].index;
        let formula = Formula::from_expr(expr, &|e| self.signal(e))?;
        let cell = logic::synthesize(&formula, expr.span)?;
        let init = init.map(|i| self.bit(i)).transpose()?.unwrap_or(0);
        let on = on.map(|e| self.pulse(e)).transpose()?.unwrap_or(Line::ONE);
        let [a, b, c, d] = cell.inputs;
        Ok(vec![
            (config::logic(k, 0), cell.table),
            (config::logic(k, 1), u16::from(a.0) | u16::from(b.0) << 6),
            (
                config::logic(k, 2),
                c.level_select() | d.level_select() << 5,
            ),
            (config::logic(k, 3), u16::from(on.0) | init << 5),
        ])
    }

    /// The shifter named by `expr`, or an error.
    pub(crate) fn shifter_index(&self, expr: &Expr) -> Result<usize> {
        match self.decl(expr)? {
            (
                _,
                Decl {
                    kind: Kind::Shifter,
                    index,
                },
            ) => Ok(index),
            (name, decl) => Err(Error::new(
                expr.span,
                format!("`{name}` is a {}, not a shifter", decl.kind.noun()),
            )),
        }
    }

    /// Connect a shifter's FIFO in one direction, once.
    pub(crate) fn claim(&mut self, k: usize, direction: Direction, span: Span) -> Result<()> {
        let fifo = &mut self.fifos[k];
        let taken = match direction {
            Direction::ToChip => std::mem::replace(&mut fifo.pull, true),
            Direction::FromChip => std::mem::replace(&mut fifo.push, true),
        };
        if taken {
            return Err(Error::new(
                span,
                "this shifter already exchanges bytes with the host in this direction",
            ));
        }
        Ok(())
    }

    fn stream(&mut self, name: &Name, from: &Expr, to: &Expr) -> Result<Stream> {
        let is_host = |e: &Expr| matches!(&e.kind, ExprKind::Name(n) if n == "host");
        let (shifter, direction) = match (is_host(from), is_host(to)) {
            (true, false) => (to, Direction::ToChip),
            (false, true) => (from, Direction::FromChip),
            _ => {
                return Err(Error::new(
                    name.span,
                    "a stream connects the host and a shifter: `host -> sender` or `receiver -> host`",
                ));
            }
        };
        let channel = self.shifter_index(shifter)?;
        self.claim(channel, direction, name.span)?;
        Ok(Stream {
            channel,
            direction,
            packets: false,
        })
    }
}
