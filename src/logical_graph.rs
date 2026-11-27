//! Pre-allocation source-connection analysis.
//!
//! This deliberately retains expressions and unresolved references; it does
//! not pretend source-level dependencies are one-to-one physical routes.
use crate::{
    LogicalExpr, LogicalExprKind, LogicalField, LogicalItem, LogicalProgram, LogicalSignal,
    LogicalStage,
};
use serde::Serialize;
use std::collections::BTreeMap;

/// Source-level category of a one-bit resource output.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum LogicalSignalKind {
    /// A value that can remain high or low across cycles.
    Level,
    /// A one-cycle event pulse.
    Pulse,
    /// A named item or output that is not a valid signal.
    NotSignal,
    /// The name is unresolved or ambiguous.
    Unknown,
}

/// Result of the subset of connection checks performed on a logical input.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum LogicalCheckStatus {
    /// The checks performed found no issue; other checks may remain.
    Valid,
    /// The source cannot satisfy the checked input contract.
    Invalid,
    /// A missing binding or unsupported expression prevents a decision.
    Unknown,
}

/// Partial validation of an input's source and pulse/level contract.
#[derive(Clone, Debug, Serialize)]
pub struct LogicalInputCheck {
    /// Result of the checks performed.
    pub status: LogicalCheckStatus,
    /// Explanatory diagnostic, if invalid or unknown.
    pub reason: Option<String>,
    /// Distinct sources of a valid logic-cell formula (`register.value`).
    /// Target cell limits, such as the current 4-input/2-pulse cell, are not
    /// applied here.
    pub formula_sources: Option<LogicalFormulaSources>,
}

/// Distinct one-bit sources read by a logic formula, constants excluded.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize)]
pub struct LogicalFormulaSources {
    /// One-cycle pulse sources.
    pub pulses: usize,
    /// Level sources.
    pub levels: usize,
}

/// A source name mentioned in an input expression.
#[derive(Clone, Debug, Serialize)]
pub struct LogicalReference {
    /// Name before physical allocation.
    pub name: String,
    /// Explicit output, when the source uses `name.output`.
    pub output: Option<String>,
    /// Resource class, `constant`, `host`, `ambiguous`, or `unresolved`.
    /// This checks the base name only, not the output's existence or type.
    pub resolution: String,
    /// Category inferred from the declared resource and output name.
    pub signal_kind: LogicalSignalKind,
}

/// One configured input expression and its possible source dependencies.
#[derive(Clone, Debug, Serialize)]
pub struct LogicalInputConnection {
    /// Consumer resource or generated stream-stage name.
    pub consumer: String,
    /// Input role; a syntactic role, not a physical selector number.
    pub port: String,
    /// Context override expression. None denotes the base configuration.
    pub context: Option<LogicalExpr>,
    /// Full input expression, when available.
    pub expression: Option<LogicalExpr>,
    /// Protocol input signal when the original expression is represented by
    /// the protocol state-machine IR, including constant zero or one.
    pub protocol_signal: Option<LogicalSignal>,
    /// Names referenced by the expression, including unresolved names.
    pub references: Vec<LogicalReference>,
    /// Partial source and input-kind validation. It is not a placement proof.
    pub check: LogicalInputCheck,
}

/// A source-level byte stream and its ordered transformations.
#[derive(Clone, Debug, Serialize)]
pub struct LogicalStreamConnection {
    /// Source stream declaration name.
    pub name: String,
    /// Source endpoint expression.
    pub source: LogicalExpr,
    /// Destination endpoint expression.
    pub destination: LogicalExpr,
    /// Source endpoint names and their base-name resolution.
    pub source_references: Vec<LogicalReference>,
    /// Destination endpoint names and their base-name resolution.
    pub destination_references: Vec<LogicalReference>,
    /// Width of each host-stream data element in the current language.
    pub element_width_bits: usize,
    /// Whether the API exposes packet boundaries and errors.
    pub packets: bool,
    /// Ordered source-level byte-processing stages.
    pub stages: Vec<LogicalStage>,
}

/// Partial logical connection graph, independent of target signal numbers.
#[derive(Clone, Debug, Serialize)]
pub struct LogicalConnectionGraph {
    /// Configured input expressions and the names they mention.
    pub inputs: Vec<LogicalInputConnection>,
    /// Stream endpoint expressions and processing stages.
    pub streams: Vec<LogicalStreamConnection>,
    /// Scope and interpretation restrictions.
    pub limitations: Vec<&'static str>,
}

impl LogicalProgram {
    /// Extract a conservative source-connection graph from the logical IR.
    /// Context entries are overrides, not simultaneous copies of resources.
    /// Each input is checked against the elaborator's pulse/level contract
    /// for its role (see [`LogicalInputCheck`]). Function-driven changes,
    /// implied/default connections and expression optimization are not modeled.
    pub fn connection_graph(&self) -> LogicalConnectionGraph {
        let mut symbols: BTreeMap<String, String> = BTreeMap::new();
        let mut declare = |name: &str, kind: &str| {
            if symbols.insert(name.to_owned(), kind.to_owned()).is_some() {
                symbols.insert(name.to_owned(), "ambiguous".into());
            }
        };
        for item in &self.items {
            match item {
                LogicalItem::Pin { name, .. } => declare(name, "pin"),
                LogicalItem::Resource {
                    name,
                    resource_kind,
                    ..
                } => declare(name, resource_kind),
                LogicalItem::Register { name, .. } => declare(name, "register"),
                LogicalItem::Events { names } => {
                    for name in names {
                        declare(name, "event");
                    }
                }
                LogicalItem::Flags { flags } => {
                    for (name, _) in flags {
                        declare(name, "flag");
                    }
                }
                LogicalItem::Protocol { name } => declare(name, "protocol"),
                LogicalItem::Machine { name, .. } => declare(name, "machine"),
                LogicalItem::Compare { name, .. } => declare(name, "compare"),
                LogicalItem::Constant { name, .. } => declare(name, "constant"),
                _ => {}
            }
        }
        symbols
            .entry("host".into())
            .or_insert_with(|| "host".into());
        let mut graph = LogicalConnectionGraph {
            inputs: Vec::new(),
            streams: Vec::new(),
            limitations: vec![
                "Signal category and simple input contracts are checked; complex expressions, output availability and expression reachability can remain unknown.",
                "Contexts are alternate configuration overrides; base and override inputs must not be treated as simultaneous.",
                "Only explicit source inputs and streams are represented; runtime reconfiguration, implicit paths, routing and cycle timing are not included.",
            ],
        };
        for item in &self.items {
            collect_item(item, None, &symbols, &mut graph);
        }
        for protocol in &self.protocols {
            for (index, source) in protocol.inputs.iter().enumerate() {
                let references = match source {
                    LogicalSignal::Constant { .. } => Vec::new(),
                    LogicalSignal::Resource { name, output } => {
                        vec![reference(name, output.clone(), &symbols)]
                    }
                };
                let check = check_input(
                    &format!("protocol.input{index}"),
                    None,
                    Some(source),
                    &symbols,
                );
                graph.inputs.push(LogicalInputConnection {
                    consumer: protocol.name.clone(),
                    port: format!("protocol.input{index}"),
                    context: None,
                    expression: None,
                    protocol_signal: Some(source.clone()),
                    references,
                    check,
                });
            }
        }
        graph
    }
}

fn reference(
    name: &str,
    output: Option<String>,
    symbols: &BTreeMap<String, String>,
) -> LogicalReference {
    let resolution = symbols
        .get(name)
        .cloned()
        .unwrap_or_else(|| "unresolved".into());
    let signal_kind = signal_kind(&resolution, output.as_deref());
    LogicalReference {
        name: name.to_owned(),
        output,
        resolution,
        signal_kind,
    }
}

fn signal_kind(class: &str, output: Option<&str>) -> LogicalSignalKind {
    use LogicalSignalKind::{Level, NotSignal, Pulse, Unknown};
    // Mirrors `Chip::signal` in `elaborate.rs`: lines 0-31 are pulses (with
    // the constants), lines 32-63 are levels.
    match (class, output) {
        ("unresolved" | "ambiguous", _) => Unknown,
        ("pin" | "register" | "flag", None) => Level,
        ("event", None) => Pulse,
        ("pin", Some("rise" | "fall")) => Pulse,
        ("timer", Some("tick" | "rise" | "fall")) => Pulse,
        ("timer", Some("out")) => Level,
        ("shifter", Some("word" | "done")) => Pulse,
        ("shifter", Some("out" | "busy")) => Level,
        ("counter", Some("reached")) => Pulse,
        ("counter", Some("at_least")) => Level,
        ("crc", Some("out" | "ok")) => Level,
        ("protocol", Some("action1" | "action2" | "action3")) => Pulse,
        // `bus.STATE` (whether STATE exists is checked by the lowering).
        ("machine", Some(_)) => Level,
        ("compare", Some("match" | "mismatch")) => Pulse,
        _ => NotSignal,
    }
}

/// What one operand of an input expression denotes.
enum Operand {
    /// Literal `0` or `1`; encoded in the pulse half of the signal space.
    Constant,
    Signal(LogicalSignalKind, String),
    Invalid(String),
    Unknown(String),
}

fn named_operand(name: &str, output: Option<&str>, symbols: &BTreeMap<String, String>) -> Operand {
    let class = symbols.get(name).map_or("unresolved", String::as_str);
    let key = match output {
        Some(output) => format!("{name}.{output}"),
        None => name.to_owned(),
    };
    match signal_kind(class, output) {
        LogicalSignalKind::Unknown => Operand::Unknown(format!("signal `{name}` is {class}")),
        LogicalSignalKind::NotSignal => {
            Operand::Invalid(format!("`{key}` is not a signal output of a {class}"))
        }
        kind => Operand::Signal(kind, key),
    }
}

/// `a.rise && b`, `a.fall && !b`, `a && b`: a pin edge or level qualified by
/// another pin's level is one signal (an I/O tile output on the reorganised
/// fabric).
fn pin_qualifier(expr: &LogicalExpr, symbols: &BTreeMap<String, String>) -> Option<Operand> {
    let LogicalExprKind::Binary { op, left, right } = &expr.expression else {
        return None;
    };
    if *op != "&&" {
        return None;
    }
    let is_pin = |n: &str| symbols.get(n).map(String::as_str) == Some("pin");
    let level = |e: &LogicalExpr| match &e.expression {
        LogicalExprKind::Name { name } => is_pin(name).then(|| name.clone()),
        LogicalExprKind::Unary { op, value } if *op == "!" => match &value.expression {
            LogicalExprKind::Name { name } if is_pin(name) => Some(name.clone()),
            _ => None,
        },
        _ => None,
    };
    let term = |e: &LogicalExpr| match &e.expression {
        LogicalExprKind::Field { object, field } if matches!(field.as_str(), "rise" | "fall") => {
            match &object.expression {
                LogicalExprKind::Name { name } if is_pin(name) => {
                    Some((name.clone(), LogicalSignalKind::Pulse))
                }
                _ => None,
            }
        }
        LogicalExprKind::Name { name } if is_pin(name) => {
            Some((name.clone(), LogicalSignalKind::Level))
        }
        _ => None,
    };
    for (a, b) in [(left, right), (right, left)] {
        if let (Some((pin, kind)), Some(other)) = (term(a), level(b))
            && pin != other
        {
            return Some(Operand::Signal(kind, format!("{pin} qualified by {other}")));
        }
    }
    None
}

fn operand(expr: &LogicalExpr, symbols: &BTreeMap<String, String>) -> Operand {
    if let Some(op) = pin_qualifier(expr, symbols) {
        return op;
    }
    match &expr.expression {
        // A selection by machine state (README, State machines) is one signal.
        LogicalExprKind::If {
            condition,
            yes: then,
            no: otherwise,
        } => {
            let state = matches!(&condition.expression, LogicalExprKind::Field { object, .. }
                if matches!(&object.expression, LogicalExprKind::Name { name }
                    if symbols.get(name).map(String::as_str) == Some("machine")));
            if !state {
                return Operand::Invalid(
                    "an `if` signal must select by a machine state (`if m.STATE { a } else { b }`)"
                        .into(),
                );
            }
            let arms = [operand(then, symbols), operand(otherwise, symbols)];
            let mut kind = None;
            for arm in arms {
                match arm {
                    Operand::Constant => {}
                    // A selection of a pulse and a level is a level where
                    // either arm is one (it can stay high).
                    Operand::Signal(k, _) => {
                        if kind != Some(LogicalSignalKind::Level) {
                            kind = Some(k);
                        }
                    }
                    other => return other,
                }
            }
            match kind {
                Some(k) => Operand::Signal(k, "a machine-state selection".into()),
                None => Operand::Constant,
            }
        }
        LogicalExprKind::Integer { value } if value == "0" || value == "1" => Operand::Constant,
        LogicalExprKind::Name { name } => named_operand(name, None, symbols),
        LogicalExprKind::Field { object, field } => match &object.expression {
            LogicalExprKind::Name { name } => named_operand(name, Some(field), symbols),
            _ => Operand::Invalid("a signal is `name` or `name.output`".into()),
        },
        _ => Operand::Invalid("expected a single signal (0, 1, `name` or `name.output`)".into()),
    }
}

/// Input contracts enforced by the current elaborator, independent of
/// selector numbering or tile counts.
#[derive(Clone, Copy)]
enum Contract {
    /// Any pulse, level or constant.
    Any,
    /// A level; constants live in pulse space and are rejected.
    Level,
    /// A pulse or constant.
    Pulse,
    /// A constant or level (pins copy one level, never a pulse).
    PinDrive,
    /// `x` or `!x` where `x` is a level.
    Hold,
    /// `ones(x)`, `zeros(x)` or `equal(x)` where `x` is a level.
    Streak,
    /// A boolean formula over signals (a logic cell).
    Formula,
    /// The name of a shifter.
    ShifterName,
    /// A source form the elaborator always rejects.
    Reject(&'static str),
}

fn contract(port: &str) -> Option<Contract> {
    use Contract::*;
    Some(match port {
        "timer.start" | "timer.stop" | "timer.run" | "timer.resync" => Any,
        "timer.hold" => Hold,
        "shifter.start" | "shifter.shift" | "shifter.sample_at" | "shifter.abort" => Any,
        "shifter.sample_data" | "shifter.end" | "shifter.stuff_while" => Level,
        "shifter.sample" => Reject("`sample` must be `<level> at <signal>`"),
        "counter.count" => Any,
        "counter.clear" => Pulse,
        "counter.streak" => Streak,
        "crc.absorb" | "crc.emit" => Any,
        "crc.restart" => Pulse,
        "crc.data" | "crc.absorb_while" | "crc.emit_while" => Level,
        "crc.link" => ShifterName,
        "register.value" => Formula,
        "register.enable" => Pulse,
        "pin.drive" => PinDrive,
        "context.select" => Level,
        "stage.end" | "stage.error" => Any,
        "stage.open" => Level,
        "machine.on" => Any,
        port if port.starts_with("protocol.input") => Any,
        _ => return None,
    })
}

fn check_input(
    port: &str,
    expr: Option<&LogicalExpr>,
    protocol: Option<&LogicalSignal>,
    symbols: &BTreeMap<String, String>,
) -> LogicalInputCheck {
    let valid = |formula_sources| LogicalInputCheck {
        status: LogicalCheckStatus::Valid,
        reason: None,
        formula_sources,
    };
    let fail = |status, reason: String| LogicalInputCheck {
        status,
        reason: Some(reason),
        formula_sources: None,
    };
    let Some(contract) = contract(port) else {
        return fail(
            LogicalCheckStatus::Unknown,
            format!("no input contract is known for `{port}`"),
        );
    };
    let (single, expr) = match (protocol, expr) {
        (Some(LogicalSignal::Constant { .. }), _) => (Operand::Constant, None),
        (Some(LogicalSignal::Resource { name, output }), _) => {
            (named_operand(name, output.as_deref(), symbols), None)
        }
        (None, Some(expr)) => (operand(expr, symbols), Some(expr)),
        (None, None) => {
            return fail(LogicalCheckStatus::Unknown, "no source expression".into());
        }
    };
    // Accept or reject one operand against a required category.
    let require = |op: Operand, allow_constant: bool, allow: &[LogicalSignalKind]| match op {
        Operand::Constant if allow_constant => valid(None),
        Operand::Constant => fail(
            LogicalCheckStatus::Invalid,
            format!("`{port}` needs a level; a constant is encoded as a pulse"),
        ),
        Operand::Signal(kind, _) if allow.contains(&kind) => valid(None),
        Operand::Signal(kind, key) => fail(
            LogicalCheckStatus::Invalid,
            format!("`{key}` is a {kind:?} but `{port}` does not accept one").to_lowercase(),
        ),
        Operand::Invalid(reason) => fail(LogicalCheckStatus::Invalid, reason),
        Operand::Unknown(reason) => fail(LogicalCheckStatus::Unknown, reason),
    };
    use LogicalSignalKind::{Level, Pulse};
    match contract {
        Contract::Any => return require(single, true, &[Level, Pulse]),
        Contract::Level => return require(single, false, &[Level]),
        Contract::Pulse => return require(single, true, &[Pulse]),
        Contract::PinDrive => return require(single, true, &[Level]),
        Contract::Reject(reason) => return fail(LogicalCheckStatus::Invalid, reason.into()),
        _ => {}
    }
    // The remaining roles inspect expression structure; protocol inputs never
    // reach them.
    let Some(expr) = expr else {
        return fail(LogicalCheckStatus::Unknown, "no source expression".into());
    };
    match contract {
        Contract::Hold => match &expr.expression {
            LogicalExprKind::Unary { op: "!", value } => {
                require(operand(value, symbols), false, &[Level])
            }
            _ => require(single, false, &[Level]),
        },
        Contract::Streak => match &expr.expression {
            LogicalExprKind::Call { callee, args }
                if matches!(&callee.expression, LogicalExprKind::Name { name }
                    if matches!(name.as_str(), "ones" | "zeros" | "equal"))
                    && args.len() == 1 =>
            {
                require(operand(&args[0].value, symbols), false, &[Level])
            }
            _ => fail(
                LogicalCheckStatus::Invalid,
                "`streak` must be `ones(level)`, `zeros(level)` or `equal(level)`".into(),
            ),
        },
        Contract::Formula => {
            let mut pulses = Vec::new();
            let mut levels = Vec::new();
            match formula(expr, symbols, &mut pulses, &mut levels) {
                Ok(()) => valid(Some(LogicalFormulaSources {
                    pulses: pulses.len(),
                    levels: levels.len(),
                })),
                Err((status, reason)) => fail(status, reason),
            }
        }
        Contract::ShifterName => match &expr.expression {
            LogicalExprKind::Name { name } => match symbols.get(name).map(String::as_str) {
                Some("shifter") => valid(None),
                None | Some("ambiguous") => fail(
                    LogicalCheckStatus::Unknown,
                    format!("CRC link target `{name}` is unresolved"),
                ),
                Some(_) => fail(
                    LogicalCheckStatus::Invalid,
                    format!("CRC link `{name}` must name a shifter"),
                ),
            },
            _ => fail(
                LogicalCheckStatus::Invalid,
                "CRC link must name a shifter".into(),
            ),
        },
        Contract::Any
        | Contract::Level
        | Contract::Pulse
        | Contract::PinDrive
        | Contract::Reject(_) => unreachable!("single-operand contracts return above"),
    }
}

/// The most decisive error among operands: invalid ahead of unknown.
fn first_error<const N: usize>(
    results: [Result<(), (LogicalCheckStatus, String)>; N],
) -> Result<(), (LogicalCheckStatus, String)> {
    let mut errors: Vec<_> = results.into_iter().filter_map(Result::err).collect();
    errors.sort_by_key(|e| e.0 != LogicalCheckStatus::Invalid);
    errors.into_iter().next().map_or(Ok(()), Err)
}

/// Check a logic-cell formula (mirrors `logic::Formula::from_expr`) and
/// collect its distinct pulse and level sources.
fn formula(
    expr: &LogicalExpr,
    symbols: &BTreeMap<String, String>,
    pulses: &mut Vec<String>,
    levels: &mut Vec<String>,
) -> Result<(), (LogicalCheckStatus, String)> {
    match &expr.expression {
        LogicalExprKind::Unary { op: "!", value } => formula(value, symbols, pulses, levels),
        LogicalExprKind::Binary { op, left, right } => {
            if !matches!(*op, "&&" | "||" | "^" | "!=" | "==" | "implies") {
                return Err((
                    LogicalCheckStatus::Invalid,
                    format!("`{op}` is not a logic operator"),
                ));
            }
            first_error([
                formula(left, symbols, pulses, levels),
                formula(right, symbols, pulses, levels),
            ])
        }
        LogicalExprKind::If { condition, yes, no } => first_error([
            formula(condition, symbols, pulses, levels),
            formula(yes, symbols, pulses, levels),
            formula(no, symbols, pulses, levels),
        ]),
        _ => match operand(expr, symbols) {
            Operand::Constant => Ok(()),
            Operand::Signal(kind, key) => {
                let set = if kind == LogicalSignalKind::Pulse {
                    pulses
                } else {
                    levels
                };
                if !set.contains(&key) {
                    set.push(key);
                }
                Ok(())
            }
            Operand::Invalid(reason) => Err((LogicalCheckStatus::Invalid, reason)),
            Operand::Unknown(reason) => Err((LogicalCheckStatus::Unknown, reason)),
        },
    }
}

fn references(
    expr: &LogicalExpr,
    symbols: &BTreeMap<String, String>,
    out: &mut Vec<LogicalReference>,
) {
    match &expr.expression {
        LogicalExprKind::Name { name } => out.push(reference(name, None, symbols)),
        LogicalExprKind::Field { object, field } => match &object.expression {
            LogicalExprKind::Name { name } => {
                out.push(reference(name, Some(field.clone()), symbols))
            }
            _ => references(object, symbols, out),
        },
        LogicalExprKind::Call { callee, args } => {
            if !matches!(&callee.expression, LogicalExprKind::Name { name } if matches!(name.as_str(), "ones" | "zeros" | "equal"))
            {
                references(callee, symbols, out);
            }
            for arg in args {
                references(&arg.value, symbols, out);
            }
        }
        LogicalExprKind::List { values } => {
            for value in values {
                references(value, symbols, out);
            }
        }
        LogicalExprKind::Unary { value, .. } => references(value, symbols, out),
        LogicalExprKind::Binary { left, right, .. } => {
            references(left, symbols, out);
            references(right, symbols, out);
        }
        LogicalExprKind::If { condition, yes, no } => {
            references(condition, symbols, out);
            references(yes, symbols, out);
            references(no, symbols, out);
        }
        LogicalExprKind::Integer { .. }
        | LogicalExprKind::Quantity { .. }
        | LogicalExprKind::String { .. } => {}
    }
}

fn add(
    consumer: &str,
    port: &str,
    context: Option<&LogicalExpr>,
    expression: &LogicalExpr,
    symbols: &BTreeMap<String, String>,
    graph: &mut LogicalConnectionGraph,
) {
    let mut refs = Vec::new();
    references(expression, symbols, &mut refs);
    let check = check_input(port, Some(expression), None, symbols);
    graph.inputs.push(LogicalInputConnection {
        consumer: consumer.to_owned(),
        port: port.to_owned(),
        context: context.cloned(),
        expression: Some(expression.clone()),
        protocol_signal: None,
        references: refs,
        check,
    });
}

fn collect_fields(
    name: &str,
    class: &str,
    fields: &[LogicalField],
    context: Option<&LogicalExpr>,
    symbols: &BTreeMap<String, String>,
    graph: &mut LogicalConnectionGraph,
) {
    for field in fields {
        let Some(value) = &field.value else {
            continue;
        };
        let field_name = field.name.as_str();
        let selected = match class {
            "timer" => matches!(field_name, "run" | "resync" | "hold"),
            "shifter" => matches!(
                field_name,
                "start" | "shift" | "sample" | "end" | "abort" | "stuff_while"
            ),
            "counter" => matches!(field_name, "count" | "clear" | "streak"),
            "crc" => matches!(
                field_name,
                "data" | "absorb" | "emit" | "restart" | "absorb_while" | "emit_while" | "link"
            ),
            "stage" => matches!(field_name, "end" | "error"),
            _ => false,
        };
        if !selected {
            continue;
        }
        // These source operators have two separate signal operands.
        if let LogicalExprKind::Binary { op, left, right } = &value.expression {
            let roles = match (class, field_name, *op) {
                ("timer", "run", "until") => Some(("timer.start", "timer.stop")),
                ("shifter", "sample", "at") => Some(("shifter.sample_data", "shifter.sample_at")),
                _ => None,
            };
            if let Some((first, second)) = roles {
                add(name, first, context, left, symbols, graph);
                add(name, second, context, right, symbols, graph);
                continue;
            }
        }
        add(
            name,
            &format!("{class}.{field_name}"),
            context,
            value,
            symbols,
            graph,
        );
    }
}

fn collect_item(
    item: &LogicalItem,
    context: Option<&LogicalExpr>,
    symbols: &BTreeMap<String, String>,
    graph: &mut LogicalConnectionGraph,
) {
    match item {
        LogicalItem::Pin {
            name,
            source: Some(source),
            ..
        } => add(name, "pin.drive", context, source, symbols, graph),
        LogicalItem::Resource {
            name,
            resource_kind,
            fields,
            ..
        } => collect_fields(name, resource_kind, fields, context, symbols, graph),
        LogicalItem::Register {
            name,
            value,
            clock_enable,
            ..
        } => {
            add(name, "register.value", context, value, symbols, graph);
            if let Some(enable) = clock_enable {
                add(name, "register.enable", context, enable, symbols, graph);
            }
        }
        LogicalItem::ContextSelector { signal } => {
            add("$context", "context.select", None, signal, symbols, graph)
        }
        LogicalItem::Machine { name, states } => {
            for state in states {
                for line in &state.on {
                    add(name, "machine.on", context, &line.event, symbols, graph);
                }
            }
        }
        LogicalItem::Context { index, body } => {
            for nested in body {
                collect_item(nested, Some(index), symbols, graph);
            }
        }
        LogicalItem::Stream {
            name,
            from,
            to,
            packets,
            element_width_bits,
            stages,
        } => {
            let mut source_references = Vec::new();
            let mut destination_references = Vec::new();
            references(from, symbols, &mut source_references);
            references(to, symbols, &mut destination_references);
            graph.streams.push(LogicalStreamConnection {
                name: name.clone(),
                source: from.clone(),
                destination: to.clone(),
                source_references,
                destination_references,
                element_width_bits: *element_width_bits,
                packets: *packets,
                stages: stages.clone(),
            });
            for (index, stage) in stages.iter().enumerate() {
                collect_fields(
                    &format!("{name}.stage{index}"),
                    "stage",
                    &stage.fields,
                    context,
                    symbols,
                    graph,
                );
            }
        }
        _ => {}
    }
}
