//! Logical program representation before target resource allocation.
//!
//! Expressions retain their structure and unresolved names. No claim of full
//! typing, routability, cycle-accurate execution, or memory independence is made.
use crate::diagnostic::Result;
use crate::syntax::ast::{Expr, ExprKind, Field, Item, Stmt, StmtKind, Type};
use crate::value::{Env, Value};
use serde::Serialize;
use std::collections::BTreeMap;

/// An unallocated program. Items and control operations retain source order.
#[derive(Clone, Debug, Serialize)]
pub struct LogicalProgram {
    /// Version of the serialized contract.
    pub schema: &'static str,
    /// Clock frequency, in hertz, as an exact decimal integer.
    pub clock_hz: String,
    /// Logical declarations, functions, test programs, and contexts.
    pub items: Vec<LogicalItem>,
    /// Validated protocol transition machines, before target binding.
    pub protocols: Vec<crate::LogicalProtocol>,
    /// Unimplemented semantic analyses that consumers must not infer.
    pub limitations: Vec<&'static str>,
}

/// Pre-allocation resource demand visible from declarations.
#[derive(Clone, Debug, Serialize)]
pub struct LogicalRequirements {
    /// Counts by logical class. Context overrides do not allocate new items.
    pub resources: BTreeMap<String, usize>,
    /// Base and explicit context shifter widths.
    pub shifter_widths: Vec<WidthRequirement>,
    /// Explicit state widths for resources such as CRCs.
    pub state_widths: Vec<WidthRequirement>,
    /// Explicit source timer periods, including context overrides.
    pub timer_timings: Vec<TimerTiming>,
    /// Function or test operations that may change a shifter frame at runtime.
    /// Such updates must be analyzed before proving an upper width bound.
    pub runtime_frame_updates: Vec<RuntimeFrameUpdate>,
    /// Scope limitations of the report.
    pub limitations: Vec<&'static str>,
}

/// A timer's explicit `every` or `first` duration before target quantization.
#[derive(Clone, Debug, Serialize)]
pub struct TimerTiming {
    /// Source-level timer name.
    pub resource: String,
    /// Context override index, or none for the base configuration.
    pub context: Option<LogicalExpr>,
    /// Timer field name (`every` or `first`).
    pub field: String,
    /// Exact cycle count or the unresolved/invalid expression.
    pub value: LogicalCycles,
}

/// A duration represented exactly in clock cycles, without target rounding.
#[derive(Clone, Debug, Serialize)]
#[serde(tag = "status", rename_all = "snake_case")]
pub enum LogicalCycles {
    /// Exact rational number of cycles.
    Known {
        /// Signed numerator, serialized as a decimal integer.
        numerator: String,
        /// Positive denominator, serialized as a decimal integer.
        denominator: String,
    },
    /// The source expression could not be evaluated with known constants.
    Unresolved {
        /// Unevaluated expression.
        expression: LogicalExpr,
    },
    /// The expression evaluated to a value that is not a duration.
    Invalid {
        /// Original expression.
        expression: LogicalExpr,
    },
}

/// A resource width in the base configuration or an explicit context override.
#[derive(Clone, Debug, Serialize)]
pub struct WidthRequirement {
    /// Source-level resource name.
    pub resource: String,
    /// Context index expression, or none for the base configuration.
    pub context: Option<LogicalExpr>,
    /// Width inference result; unknown values retain their expression.
    pub width: LogicalWidth,
}

/// A source operation that may change a frame at runtime.
#[derive(Clone, Debug, Serialize)]
pub struct RuntimeFrameUpdate {
    /// Function or test containing the update.
    pub owner: String,
    /// Update target expression; it is not assumed to resolve to a shifter.
    pub target: LogicalExpr,
    /// Source span of the update.
    pub source_span: [usize; 2],
}

impl LogicalProgram {
    /// Summarize logical resource counts and declared shifter widths without
    /// allocating physical instances. This report does not establish that
    /// the source names resolve, functions are reachable, or the design fits.
    pub fn requirements(&self) -> LogicalRequirements {
        let mut result = LogicalRequirements {
            resources: BTreeMap::new(),
            shifter_widths: Vec::new(),
            state_widths: Vec::new(),
            timer_timings: Vec::new(),
            runtime_frame_updates: Vec::new(),
            limitations: vec![
                "Counts include declared resources, including unused resources, and exclude implicit backend resources.",
                "Contexts are alternate overrides; runtime updates are potential operations, not reachability results.",
                "Widths, counts and expressions do not establish resource compatibility, routing or timing.",
            ],
        };
        for item in &self.items {
            match item {
                LogicalItem::Pin { .. } => *result.resources.entry("pin".into()).or_default() += 1,
                LogicalItem::Resource {
                    name,
                    resource_kind,
                    payload_width,
                    state_width,
                    timings,
                    ..
                } => {
                    *result.resources.entry(resource_kind.clone()).or_default() += 1;
                    if let Some(width) = payload_width {
                        result.shifter_widths.push(WidthRequirement {
                            resource: name.clone(),
                            context: None,
                            width: width.clone(),
                        });
                    }
                    if let Some(width) = state_width {
                        result.state_widths.push(WidthRequirement {
                            resource: name.clone(),
                            context: None,
                            width: width.clone(),
                        });
                    }
                    result
                        .timer_timings
                        .extend(timings.iter().map(|(field, value)| TimerTiming {
                            resource: name.clone(),
                            context: None,
                            field: field.clone(),
                            value: value.clone(),
                        }));
                }
                LogicalItem::Register { .. } => {
                    *result.resources.entry("register".into()).or_default() += 1
                }
                LogicalItem::Events { names } => {
                    *result.resources.entry("event".into()).or_default() += names.len()
                }
                LogicalItem::Flags { flags } => {
                    *result.resources.entry("flag".into()).or_default() += flags.len()
                }
                LogicalItem::Protocol { .. } => {
                    *result.resources.entry("protocol".into()).or_default() += 1
                }
                LogicalItem::Context { index, body } => {
                    for resource in body {
                        if let LogicalItem::Resource {
                            name,
                            payload_width,
                            state_width,
                            timings,
                            ..
                        } = resource
                        {
                            if let Some(width) = payload_width {
                                result.shifter_widths.push(WidthRequirement {
                                    resource: name.clone(),
                                    context: Some(index.clone()),
                                    width: width.clone(),
                                });
                            }
                            if let Some(width) = state_width {
                                result.state_widths.push(WidthRequirement {
                                    resource: name.clone(),
                                    context: Some(index.clone()),
                                    width: width.clone(),
                                });
                            }
                            result
                                .timer_timings
                                .extend(timings.iter().map(|(field, value)| TimerTiming {
                                    resource: name.clone(),
                                    context: Some(index.clone()),
                                    field: field.clone(),
                                    value: value.clone(),
                                }));
                        }
                    }
                }
                LogicalItem::Function { name, body, .. } | LogicalItem::Test { name, body, .. } => {
                    collect_frame_updates(name, body, &mut result.runtime_frame_updates);
                }
                _ => {}
            }
        }
        result
    }
}

fn collect_frame_updates(
    owner: &str,
    operations: &[LogicalOperation],
    updates: &mut Vec<RuntimeFrameUpdate>,
) {
    for op in operations {
        match &op.operation {
            LogicalOperationKind::Set { target, fields }
                if fields.iter().any(|field| field.name == "frame") =>
            {
                updates.push(RuntimeFrameUpdate {
                    owner: owner.to_owned(),
                    target: target.clone(),
                    source_span: op.source_span,
                });
            }
            LogicalOperationKind::Assign { target, .. } if matches!(&target.expression, LogicalExprKind::Field { field, .. } if field == "frame") =>
            {
                updates.push(RuntimeFrameUpdate {
                    owner: owner.to_owned(),
                    target: target.clone(),
                    source_span: op.source_span,
                });
            }
            LogicalOperationKind::Repeat { body, .. }
            | LogicalOperationKind::For { body, .. }
            | LogicalOperationKind::Parallel { body } => {
                collect_frame_updates(owner, body, updates)
            }
            LogicalOperationKind::If {
                then, otherwise, ..
            } => {
                collect_frame_updates(owner, then, updates);
                collect_frame_updates(owner, otherwise, updates);
            }
            _ => {}
        }
    }
}

/// Expression tree before runtime evaluation and target binding.
#[derive(Clone, Debug, Serialize)]
#[serde(tag = "kind", rename_all = "snake_case")]
#[allow(missing_docs)]
pub enum LogicalExprKind {
    Integer {
        value: String,
    },
    Quantity {
        value: String,
        unit: String,
    },
    String {
        value: String,
    },
    Name {
        name: String,
    },
    Field {
        object: Box<LogicalExpr>,
        field: String,
    },
    Call {
        callee: Box<LogicalExpr>,
        args: Vec<LogicalArgument>,
    },
    List {
        values: Vec<LogicalExpr>,
    },
    Unary {
        op: &'static str,
        value: Box<LogicalExpr>,
    },
    Binary {
        op: &'static str,
        left: Box<LogicalExpr>,
        right: Box<LogicalExpr>,
    },
    If {
        condition: Box<LogicalExpr>,
        yes: Box<LogicalExpr>,
        no: Box<LogicalExpr>,
    },
}

/// Expression and the half-open byte range in the source file.
#[derive(Clone, Debug, Serialize)]
pub struct LogicalExpr {
    /// Source byte range `[start, end)`.
    pub source_span: [usize; 2],
    /// Expression operation and operands.
    #[serde(flatten)]
    pub expression: LogicalExprKind,
}

/// A positional or named argument to an expression call.
#[derive(Clone, Debug, Serialize)]
pub struct LogicalArgument {
    /// Name, or none for a positional argument.
    pub name: Option<String>,
    /// Argument value expression.
    pub value: LogicalExpr,
}

/// Resource or pipeline configuration property.
#[derive(Clone, Debug, Serialize)]
pub struct LogicalField {
    /// Property name.
    pub name: String,
    /// Value, or none for a bare flag.
    pub value: Option<LogicalExpr>,
}

/// Width inferred from a logical resource, without target limits.
#[derive(Clone, Debug, Serialize)]
#[serde(tag = "status", rename_all = "snake_case")]
#[allow(missing_docs)]
pub enum LogicalWidth {
    Known { bits: String },
    Symbolic { expression: LogicalExpr },
    Invalid { reason: &'static str },
}

/// One logical byte stage and its source configuration.
#[derive(Clone, Debug, Serialize)]
pub struct LogicalStage {
    /// Stage operation name.
    pub operation: String,
    /// Ordered configuration properties.
    pub fields: Vec<LogicalField>,
}

/// Function parameter or return type as written in the source language.
#[derive(Clone, Debug, Serialize)]
pub struct LogicalType {
    /// Source type name; types have not yet been lowered to hardware widths.
    pub name: String,
    /// Optional transport resource (`bytes via NAME`).
    pub via: Option<String>,
    /// Optional inclusive range expressions.
    pub range: Option<(LogicalExpr, LogicalExpr)>,
}

/// One source-level control or test operation.
#[derive(Clone, Debug, Serialize)]
pub struct LogicalOperation {
    /// Source byte range `[start, end)`.
    pub source_span: [usize; 2],
    /// Operation and its typed operands and regions.
    #[serde(flatten)]
    pub operation: LogicalOperationKind,
}

/// Structured control operations. Runtime order follows source order.
#[derive(Clone, Debug, Serialize)]
#[serde(tag = "op", rename_all = "snake_case")]
#[allow(missing_docs)]
pub enum LogicalOperationKind {
    Fire {
        event: String,
    },
    Wait {
        signal: LogicalExpr,
        next: bool,
    },
    Repeat {
        count: LogicalExpr,
        body: Vec<LogicalOperation>,
    },
    For {
        variable: String,
        iterable: LogicalExpr,
        body: Vec<LogicalOperation>,
    },
    Parallel {
        body: Vec<LogicalOperation>,
    },
    If {
        condition: LogicalExpr,
        then: Vec<LogicalOperation>,
        otherwise: Vec<LogicalOperation>,
    },
    Reply {
        value: LogicalExpr,
    },
    Fail {
        message: String,
    },
    Expect {
        condition: LogicalExpr,
        fails: Option<String>,
    },
    Attach {
        peer: LogicalExpr,
        alias: Option<String>,
    },
    Wire {
        from: LogicalExpr,
        to: LogicalExpr,
    },
    Set {
        target: LogicalExpr,
        fields: Vec<LogicalField>,
    },
    Assign {
        target: LogicalExpr,
        value: LogicalExpr,
    },
    Evaluate {
        expression: LogicalExpr,
    },
}

/// Ordered program declarations. No physical resource indices are included.
#[derive(Clone, Debug, Serialize)]
#[serde(tag = "kind", rename_all = "snake_case")]
#[allow(missing_docs)]
pub enum LogicalItem {
    Clock {
        value: LogicalExpr,
    },
    Constant {
        name: String,
        value: LogicalExpr,
    },
    Pin {
        name: String,
        pads: Option<Vec<LogicalExpr>>,
        mode: String,
        source: Option<LogicalExpr>,
    },
    Resource {
        name: String,
        resource_kind: String,
        fields: Vec<LogicalField>,
        payload_width: Option<LogicalWidth>,
        state_width: Option<LogicalWidth>,
        timings: Vec<(String, LogicalCycles)>,
    },
    Register {
        name: String,
        initial: Option<LogicalExpr>,
        clock_enable: Option<LogicalExpr>,
        value: LogicalExpr,
    },
    Events {
        names: Vec<String>,
    },
    Flags {
        flags: Vec<(String, Option<LogicalExpr>)>,
    },
    Protocol {
        name: String,
    },
    Stream {
        name: String,
        from: LogicalExpr,
        to: LogicalExpr,
        packets: bool,
        element_width_bits: usize,
        stages: Vec<LogicalStage>,
    },
    Function {
        name: String,
        exported: bool,
        params: Vec<(String, LogicalType)>,
        returns: Option<LogicalType>,
        body: Vec<LogicalOperation>,
    },
    Test {
        name: String,
        timeout: Option<LogicalExpr>,
        body: Vec<LogicalOperation>,
    },
    Invariant {
        name: String,
        condition: LogicalExpr,
    },
    ContextSelector {
        signal: LogicalExpr,
    },
    Context {
        index: LogicalExpr,
        body: Vec<LogicalItem>,
    },
}

fn expr(e: &Expr) -> LogicalExpr {
    let expression = match &e.kind {
        ExprKind::Int(n) => LogicalExprKind::Integer {
            value: n.to_string(),
        },
        ExprKind::Quantity(n, unit) => LogicalExprKind::Quantity {
            value: n.to_string(),
            unit: unit.clone(),
        },
        ExprKind::Str(value) => LogicalExprKind::String {
            value: value.clone(),
        },
        ExprKind::Name(name) => LogicalExprKind::Name { name: name.clone() },
        ExprKind::Field(object, name) => LogicalExprKind::Field {
            object: Box::new(expr(object)),
            field: name.text.clone(),
        },
        ExprKind::Call(callee, args) => LogicalExprKind::Call {
            callee: Box::new(expr(callee)),
            args: args
                .iter()
                .map(|a| LogicalArgument {
                    name: a.name.as_ref().map(|n| n.text.clone()),
                    value: expr(&a.value),
                })
                .collect(),
        },
        ExprKind::List(values) => LogicalExprKind::List {
            values: values.iter().map(expr).collect(),
        },
        ExprKind::Unary(op, value) => LogicalExprKind::Unary {
            op,
            value: Box::new(expr(value)),
        },
        ExprKind::Binary(op, left, right) => LogicalExprKind::Binary {
            op,
            left: Box::new(expr(left)),
            right: Box::new(expr(right)),
        },
        ExprKind::If(condition, yes, no) => LogicalExprKind::If {
            condition: Box::new(expr(condition)),
            yes: Box::new(expr(yes)),
            no: Box::new(expr(no)),
        },
    };
    LogicalExpr {
        source_span: [e.span.start, e.span.end],
        expression,
    }
}

fn fields(fields: &[Field]) -> Vec<LogicalField> {
    fields
        .iter()
        .map(|f| LogicalField {
            name: f.name.text.clone(),
            value: f.value.as_ref().map(expr),
        })
        .collect()
}

fn ty(t: &Type) -> LogicalType {
    LogicalType {
        name: t.name.text.clone(),
        via: t.via.as_ref().map(|n| n.text.clone()),
        range: t.range.as_ref().map(|(a, b)| (expr(a), expr(b))),
    }
}

fn operations(body: &[Stmt]) -> Vec<LogicalOperation> {
    body.iter()
        .map(|stmt| {
            let operation = match &stmt.kind {
                StmtKind::Fire(n) => LogicalOperationKind::Fire {
                    event: n.text.clone(),
                },
                StmtKind::Wait { signal, next } => LogicalOperationKind::Wait {
                    signal: expr(signal),
                    next: *next,
                },
                StmtKind::Repeat { count, body } => LogicalOperationKind::Repeat {
                    count: expr(count),
                    body: operations(body),
                },
                StmtKind::For { var, iter, body } => LogicalOperationKind::For {
                    variable: var.text.clone(),
                    iterable: expr(iter),
                    body: operations(body),
                },
                StmtKind::Parallel(body) => LogicalOperationKind::Parallel {
                    body: operations(body),
                },
                StmtKind::If {
                    condition,
                    then,
                    otherwise,
                } => LogicalOperationKind::If {
                    condition: expr(condition),
                    then: operations(then),
                    otherwise: operations(otherwise),
                },
                StmtKind::Reply(value) => LogicalOperationKind::Reply { value: expr(value) },
                StmtKind::Fail(message) => LogicalOperationKind::Fail {
                    message: message.text.clone(),
                },
                StmtKind::Expect { condition, fails } => LogicalOperationKind::Expect {
                    condition: expr(condition),
                    fails: fails.as_ref().map(|n| n.text.clone()),
                },
                StmtKind::Attach { peer, alias } => LogicalOperationKind::Attach {
                    peer: expr(peer),
                    alias: alias.as_ref().map(|n| n.text.clone()),
                },
                StmtKind::Wire { from, to } => LogicalOperationKind::Wire {
                    from: expr(from),
                    to: expr(to),
                },
                StmtKind::Set { target, fields: f } => LogicalOperationKind::Set {
                    target: expr(target),
                    fields: fields(f),
                },
                StmtKind::Assign { target, value } => LogicalOperationKind::Assign {
                    target: expr(target),
                    value: expr(value),
                },
                StmtKind::Expr(value) => LogicalOperationKind::Evaluate {
                    expression: expr(value),
                },
            };
            LogicalOperation {
                source_span: [stmt.span.start, stmt.span.end],
                operation,
            }
        })
        .collect()
}

fn shifter_width(fields: &[Field], env: &Env) -> LogicalWidth {
    let Some(frame) = fields.iter().find(|f| f.name.text == "frame") else {
        return LogicalWidth::Known { bits: "8".into() };
    };
    let Some(Expr {
        kind: ExprKind::List(parts),
        ..
    }) = frame.value.as_ref()
    else {
        return LogicalWidth::Invalid {
            reason: "frame must be a list",
        };
    };
    let widths: Vec<_> = parts.iter().filter_map(|part| match &part.kind {
        ExprKind::Call(callee, args) if matches!(&callee.kind, ExprKind::Name(name) if name == "data") => Some(args.first()),
        _ => None,
    }).collect();
    if widths.len() != 1 {
        return LogicalWidth::Invalid {
            reason: "frame must have exactly one data field",
        };
    }
    let Some(arg) = widths[0] else {
        return LogicalWidth::Invalid {
            reason: "data field requires a width",
        };
    };
    match env.integer(&arg.value) {
        Ok(bits) if bits > 0 => LogicalWidth::Known {
            bits: bits.to_string(),
        },
        Ok(_) => LogicalWidth::Invalid {
            reason: "data width must be positive",
        },
        Err(_) => LogicalWidth::Symbolic {
            expression: expr(&arg.value),
        },
    }
}

fn timer_timings(fields: &[Field], env: &Env) -> Vec<(String, LogicalCycles)> {
    fields
        .iter()
        .filter_map(|field| {
            if !matches!(field.name.text.as_str(), "every" | "first") {
                return None;
            }
            let value = field.value.as_ref()?;
            let result = match env.eval(value) {
                Ok(Value::Cycles(ratio)) => {
                    let (numerator, denominator) = ratio.parts();
                    LogicalCycles::Known {
                        numerator: numerator.to_string(),
                        denominator: denominator.to_string(),
                    }
                }
                Ok(_) => LogicalCycles::Invalid {
                    expression: expr(value),
                },
                Err(_) => LogicalCycles::Unresolved {
                    expression: expr(value),
                },
            };
            Some((field.name.text.clone(), result))
        })
        .collect()
}

fn state_width(fields: &[Field], env: &Env) -> Option<LogicalWidth> {
    let field = fields.iter().find(|field| field.name.text == "width")?;
    let Some(value) = field.value.as_ref() else {
        return Some(LogicalWidth::Invalid {
            reason: "width requires a value",
        });
    };
    Some(match env.integer(value) {
        Ok(bits) if bits > 0 => LogicalWidth::Known {
            bits: bits.to_string(),
        },
        Ok(_) => LogicalWidth::Invalid {
            reason: "state width must be positive",
        },
        Err(_) => LogicalWidth::Symbolic {
            expression: expr(value),
        },
    })
}

fn item(source: &Item, env: &Env, is_context_override: bool) -> LogicalItem {
    match source {
        Item::Clock(value) => LogicalItem::Clock { value: expr(value) },
        Item::Const { name, value } => LogicalItem::Constant {
            name: name.text.clone(),
            value: expr(value),
        },
        Item::Pin {
            name,
            pads,
            mode,
            source,
        } => LogicalItem::Pin {
            name: name.text.clone(),
            pads: pads.as_ref().map(|p| p.iter().map(expr).collect()),
            mode: mode.text.clone(),
            source: source.as_ref().map(expr),
        },
        Item::Tile {
            kind,
            name,
            fields: f,
        } => LogicalItem::Resource {
            name: name.text.clone(),
            resource_kind: kind.text.clone(),
            fields: fields(f),
            payload_width: (kind.text == "shifter"
                && (!is_context_override || f.iter().any(|field| field.name.text == "frame")))
            .then(|| shifter_width(f, env)),
            state_width: if kind.text == "crc" {
                state_width(f, env)
            } else {
                None
            },
            timings: if kind.text == "timer" {
                timer_timings(f, env)
            } else {
                Vec::new()
            },
        },
        Item::Reg {
            name,
            init,
            on,
            value,
        } => LogicalItem::Register {
            name: name.text.clone(),
            initial: init.as_ref().map(expr),
            clock_enable: on.as_ref().map(expr),
            value: expr(value),
        },
        Item::Event(names) => LogicalItem::Events {
            names: names.iter().map(|n| n.text.clone()).collect(),
        },
        Item::Flag(flags) => LogicalItem::Flags {
            flags: flags
                .iter()
                .map(|(n, v)| (n.text.clone(), v.as_ref().map(expr)))
                .collect(),
        },
        Item::Protocol { name, .. } => LogicalItem::Protocol {
            name: name.text.clone(),
        },
        Item::Stream {
            name,
            from,
            to,
            packets,
            stages,
        } => LogicalItem::Stream {
            name: name.text.clone(),
            from: expr(from),
            to: expr(to),
            packets: *packets,
            element_width_bits: 8,
            stages: stages
                .iter()
                .map(|s| LogicalStage {
                    operation: s.kind.text.clone(),
                    fields: fields(&s.fields),
                })
                .collect(),
        },
        Item::Fn {
            exported,
            name,
            params,
            returns,
            body,
        } => LogicalItem::Function {
            name: name.text.clone(),
            exported: *exported,
            params: params
                .iter()
                .map(|p| (p.name.text.clone(), ty(&p.ty)))
                .collect(),
            returns: returns.as_ref().map(ty),
            body: operations(body),
        },
        Item::Test {
            name,
            body,
            step_timeout,
        } => LogicalItem::Test {
            name: name.text.clone(),
            timeout: step_timeout.as_ref().map(expr),
            body: operations(body),
        },
        Item::Always { name, condition } => LogicalItem::Invariant {
            name: name.text.clone(),
            condition: expr(condition),
        },
        Item::Contexts(signal) => LogicalItem::ContextSelector {
            signal: expr(signal),
        },
        Item::Context { index, body } => LogicalItem::Context {
            index: expr(index),
            body: body.iter().map(|nested| item(nested, env, true)).collect(),
        },
    }
}

/// Extract a source-level logical program without assigning target resources.
/// Name resolution, most type checking, and hardware support are not implied.
pub fn extract_logical_program(source: &str) -> Result<LogicalProgram> {
    let items = crate::syntax::parse(source)?;
    extract(&items)
}

pub(crate) fn extract(items: &[Item]) -> Result<LogicalProgram> {
    let env = crate::protocol_ir::environment(items)?;
    let protocols = crate::protocol_ir::extract_with_env(items, &env)?;
    Ok(LogicalProgram {
        schema: "spatial.logical-program-ir.v1",
        clock_hz: env.clock_hz.to_string(),
        items: items.iter().map(|i| item(i, &env, false)).collect(),
        protocols,
        limitations: vec![
            "Names and types are not fully resolved.",
            "Expressions and operations are not cycle-accurate execution models.",
            "No resource allocation, memory topology, routing or physical feasibility is implied.",
        ],
    })
}
