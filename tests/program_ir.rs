//! Source-level program IR, widths, and preserved control structure.
use spatial::{
    LogicalExprKind, LogicalItem, LogicalOperationKind, LogicalWidth, compile,
    extract_logical_program,
};

#[test]
fn captures_wide_and_symbolic_words_without_target_allocation() {
    let src = "const W = 24 shifter wide { frame: [0, data(W), 1] } shifter dynamic { frame: [data(runtime)] } export stream tx: host -> wide";
    let ir = extract_logical_program(src).unwrap();
    let widths: Vec<_> = ir
        .items
        .iter()
        .filter_map(|item| match item {
            LogicalItem::Resource {
                payload_width: Some(width),
                ..
            } => Some(width),
            _ => None,
        })
        .collect();
    assert!(matches!(widths[0], LogicalWidth::Known { bits } if bits == "24"));
    assert!(matches!(widths[1], LogicalWidth::Symbolic { .. }));
    assert!(ir.items.iter().any(|item| matches!(
        item,
        LogicalItem::Stream {
            element_width_bits: 8,
            ..
        }
    )));
    assert!(compile(src).is_err());
}

#[test]
fn records_control_regions_contexts_and_source_spans() {
    let src = include_str!("../examples/i2c.spt");
    let ir = extract_logical_program(src).unwrap();
    let function = ir
        .items
        .iter()
        .find_map(|item| match item {
            LogicalItem::Function { name, body, .. } if name == "write_read" => Some(body),
            _ => None,
        })
        .unwrap();
    assert!(
        function
            .iter()
            .any(|op| matches!(op.operation, LogicalOperationKind::For { .. }))
    );
    assert!(
        function
            .iter()
            .any(|op| matches!(op.operation, LogicalOperationKind::Repeat { .. }))
    );
    assert!(
        function
            .iter()
            .all(|op| op.source_span[0] < op.source_span[1] && op.source_span[1] <= src.len())
    );
    let context_ir =
        extract_logical_program(include_str!("../examples/contexts_logic.spt")).unwrap();
    assert!(
        context_ir
            .items
            .iter()
            .any(|item| matches!(item, LogicalItem::ContextSelector { .. }))
    );
    assert_eq!(
        context_ir
            .items
            .iter()
            .filter(|item| matches!(item, LogicalItem::Context { .. }))
            .count(),
        2
    );
}

#[test]
fn stream_pipeline_and_expression_dependencies_remain_ordered() {
    let ir = extract_logical_program(include_str!("../examples/byte_framing.spt")).unwrap();
    let stages = ir
        .items
        .iter()
        .find_map(|item| match item {
            LogicalItem::Stream { name, stages, .. } if name == "send" => Some(stages),
            _ => None,
        })
        .unwrap();
    assert_eq!(
        stages
            .iter()
            .map(|s| s.operation.as_str())
            .collect::<Vec<_>>(),
        ["checksum", "escape"]
    );
    let sender = ir
        .items
        .iter()
        .find_map(|item| match item {
            LogicalItem::Resource { name, fields, .. } if name == "sender" => Some(fields),
            _ => None,
        })
        .unwrap();
    let shift = sender
        .iter()
        .find(|f| f.name == "shift")
        .unwrap()
        .value
        .as_ref()
        .unwrap();
    assert!(matches!(&shift.expression, LogicalExprKind::Field { field, .. } if field == "tick"));
}

#[test]
fn all_checked_in_example_programs_have_logical_ir() {
    for entry in std::fs::read_dir("examples").unwrap() {
        let path = entry.unwrap().path();
        if path.extension().is_none_or(|extension| extension != "spt") {
            continue;
        }
        let source = std::fs::read_to_string(&path).unwrap();
        extract_logical_program(&source)
            .unwrap_or_else(|error| panic!("{}: {error}", path.display()));
    }
}

#[test]
fn requirements_preserve_context_reuse_and_flag_runtime_reconfiguration() {
    let ir = extract_logical_program(include_str!("../examples/i2c.spt")).unwrap();
    let requirements = ir.requirements();
    assert_eq!(requirements.resources["shifter"], 2);
    assert_eq!(requirements.resources["timer"], 2);
    assert!(
        requirements
            .runtime_frame_updates
            .iter()
            .any(|update| update.owner == "condition")
    );
    assert!(
        requirements
            .runtime_frame_updates
            .iter()
            .any(|update| update.owner == "byte")
    );
    let ctx = extract_logical_program(include_str!("../examples/contexts_logic.spt"))
        .unwrap()
        .requirements();
    assert_eq!(ctx.resources["register"], 2);
}

#[test]
fn preserves_exact_fractional_and_unresolved_timer_durations() {
    use spatial::LogicalCycles;
    let ir = extract_logical_program(
        "clock 50 MHz const PERIOD = 10 ns timer t { every: PERIOD, first: dynamic, run: 1 }",
    )
    .unwrap();
    let timings = ir.requirements().timer_timings;
    assert_eq!(timings.len(), 2);
    assert!(
        matches!(&timings[0].value, LogicalCycles::Known { numerator, denominator } if numerator == "1" && denominator == "2")
    );
    assert!(matches!(
        &timings[1].value,
        LogicalCycles::Unresolved { .. }
    ));
}

#[test]
fn crc_widths_are_logical_and_not_limited_by_current_hardware() {
    let ir = extract_logical_program("crc c { width: 64, poly: 27, check: 0 }").unwrap();
    let requirements = ir.requirements();
    assert_eq!(requirements.resources["crc"], 1);
    assert!(
        matches!(&requirements.state_widths[0].width, LogicalWidth::Known { bits } if bits == "64")
    );
    assert!(compile("crc c { width: 64, poly: 27, check: 0 }").is_err());
}
