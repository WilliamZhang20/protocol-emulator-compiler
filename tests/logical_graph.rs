//! Pre-allocation graph roles and preservation of unresolved sources.
use spatial::{compile, extract_logical_program};

#[test]
fn uart_signal_roles_and_streams_follow_source_and_compiler_mapping() {
    let source = include_str!("../examples/uart.spt");
    let ir = extract_logical_program(source).unwrap();
    let graph = ir.connection_graph();
    let find = |consumer: &str, port: &str| {
        graph
            .inputs
            .iter()
            .find(|edge| edge.consumer == consumer && edge.port == port)
            .unwrap()
    };
    let shift = find("sender", "shifter.shift");
    assert_eq!(shift.references[0].name, "tx_bit");
    assert_eq!(shift.references[0].output.as_deref(), Some("tick"));
    assert_eq!(shift.references[0].resolution, "timer");
    assert_eq!(
        find("receiver", "shifter.sample_data").references[0].name,
        "rx"
    );
    assert_eq!(
        find("receiver", "shifter.sample_at").references[0].name,
        "rx_bit"
    );
    assert_eq!(graph.streams.len(), 2);
    assert_eq!(graph.streams[0].element_width_bits, 8);
    let build = compile(source).unwrap();
    assert!(
        build
            .signal_connections
            .iter()
            .any(|edge| edge.source.as_deref() == Some("tx_bit")
                && edge.destination == "sender"
                && edge.input == "shifter.shift")
    );
}

#[test]
fn reports_unresolved_names_and_context_overrides_without_merging_them() {
    let source = "pin x: out = missing timer t { every: 10 cycles, run: missing }";
    let graph = extract_logical_program(source).unwrap().connection_graph();
    assert!(
        graph
            .inputs
            .iter()
            .flat_map(|input| &input.references)
            .any(|reference| reference.name == "missing" && reference.resolution == "unresolved")
    );
    let context = extract_logical_program(include_str!("../examples/contexts_logic.spt"))
        .unwrap()
        .connection_graph();
    assert!(
        context
            .inputs
            .iter()
            .any(|edge| edge.consumer == "tx_drive" && edge.context.is_none())
    );
    assert_eq!(
        context
            .inputs
            .iter()
            .filter(|edge| edge.consumer == "tx_drive" && edge.context.is_some())
            .count(),
        2
    );
}

#[test]
fn protocol_and_crc_links_keep_logical_resource_names() {
    let source = "timer t { every: 10 cycles, pulse, run: 1 } protocol p on t.tick { transition 0 high -> 0 action 1 }";
    let graph = extract_logical_program(source).unwrap().connection_graph();
    let protocol = graph
        .inputs
        .iter()
        .find(|edge| edge.consumer == "p")
        .unwrap();
    assert_eq!(protocol.references[0].name, "t");
    assert_eq!(protocol.references[0].output.as_deref(), Some("tick"));
    let crc_graph = extract_logical_program(include_str!("../examples/ethernet_tx_coded.spt"))
        .unwrap()
        .connection_graph();
    assert!(
        crc_graph.inputs.iter().any(|edge| edge.port == "crc.link"
            && edge.references.iter().any(|r| r.resolution == "shifter"))
    );
}

use spatial::{LogicalCheckStatus, LogicalConnectionGraph, LogicalFormulaSources};

fn graph(source: &str) -> LogicalConnectionGraph {
    extract_logical_program(source).unwrap().connection_graph()
}

/// Every input of every checked-in example passes the logical contract checks
/// and none is left undecided. Examples are known-good compiler inputs.
#[test]
fn example_corpus_inputs_are_all_valid() {
    let mut checked = 0;
    for entry in std::fs::read_dir(concat!(env!("CARGO_MANIFEST_DIR"), "/examples")).unwrap() {
        let path = entry.unwrap().path();
        if path.extension().is_none_or(|e| e != "spt") {
            continue;
        }
        let source = std::fs::read_to_string(&path).unwrap();
        for input in graph(&source).inputs {
            assert_eq!(
                input.check.status,
                LogicalCheckStatus::Valid,
                "{}: {} {} -> {:?}",
                path.display(),
                input.consumer,
                input.port,
                input.check.reason
            );
            checked += 1;
        }
    }
    assert!(checked > 300, "only {checked} inputs checked");
}

/// A shared harness: one varied line on top of resources that compile.
const BASE: &str = "clock 50 MHz
pin rx: in
pin out0: out = 0
flag f = 0
event go
timer t { every: 64 cycles, pulse, run: 1 }
shifter s { frame: [0, data(8), 1], idle: 1, shift: t.tick }
";

/// The logical check must reject a program exactly when the elaborator does.
/// Each case changes one input; `port` names the input under test.
#[test]
fn input_contracts_agree_with_the_elaborator() {
    let cases: &[(&str, &str, &str, LogicalCheckStatus)] = &[
        // Pins copy one level or a constant, never a pulse or a formula.
        (
            "pin p: out = f",
            "p",
            "pin.drive",
            LogicalCheckStatus::Valid,
        ),
        (
            "pin p: out = 1",
            "p",
            "pin.drive",
            LogicalCheckStatus::Valid,
        ),
        (
            "pin p: out = s.busy",
            "p",
            "pin.drive",
            LogicalCheckStatus::Valid,
        ),
        (
            "pin p: out = t.tick",
            "p",
            "pin.drive",
            LogicalCheckStatus::Invalid,
        ),
        (
            "pin p: out = f && rx",
            "p",
            "pin.drive",
            LogicalCheckStatus::Invalid,
        ),
        (
            "pin p: out = t",
            "p",
            "pin.drive",
            LogicalCheckStatus::Invalid,
        ),
        (
            "pin p: out = s.nope",
            "p",
            "pin.drive",
            LogicalCheckStatus::Invalid,
        ),
        // Registers accept formulas over any signals; `on` needs a pulse.
        (
            "reg r = f ^ !rx",
            "r",
            "register.value",
            LogicalCheckStatus::Valid,
        ),
        (
            "reg r = if f { t.tick } else { go }",
            "r",
            "register.value",
            LogicalCheckStatus::Valid,
        ),
        (
            "reg r = f + rx",
            "r",
            "register.value",
            LogicalCheckStatus::Invalid,
        ),
        (
            "reg r = f && t",
            "r",
            "register.value",
            LogicalCheckStatus::Invalid,
        ),
        (
            "reg r on t.tick = rx",
            "r",
            "register.enable",
            LogicalCheckStatus::Valid,
        ),
        (
            "reg r on rx = rx",
            "r",
            "register.enable",
            LogicalCheckStatus::Invalid,
        ),
        // Timer hold reads a level, optionally inverted; constants are pulses.
        (
            "timer h { every: 8 cycles, run: 1, hold: !f }",
            "h",
            "timer.hold",
            LogicalCheckStatus::Valid,
        ),
        (
            "timer h { every: 8 cycles, run: 1, hold: t.tick }",
            "h",
            "timer.hold",
            LogicalCheckStatus::Invalid,
        ),
        (
            "timer h { every: 8 cycles, run: 1, hold: 1 }",
            "h",
            "timer.hold",
            LogicalCheckStatus::Invalid,
        ),
        (
            "timer h { every: 8 cycles, run: go until t.tick }",
            "h",
            "timer.start",
            LogicalCheckStatus::Valid,
        ),
        (
            "timer h { every: 8 cycles, run: 2 }",
            "h",
            "timer.run",
            LogicalCheckStatus::Invalid,
        ),
        // Counters: `clear` is a pulse, `streak` is ones/zeros/equal(level).
        (
            "counter c { count: t.tick, streak: ones(rx), clear: go }",
            "c",
            "counter.streak",
            LogicalCheckStatus::Valid,
        ),
        (
            "counter c { count: t.tick, streak: ones(t.tick) }",
            "c",
            "counter.streak",
            LogicalCheckStatus::Invalid,
        ),
        (
            "counter c { count: t.tick, streak: some(rx) }",
            "c",
            "counter.streak",
            LogicalCheckStatus::Invalid,
        ),
        (
            "counter c { count: t.tick, clear: rx }",
            "c",
            "counter.clear",
            LogicalCheckStatus::Invalid,
        ),
        // CRC `data` is a level; `restart` is a pulse; `link` names a shifter.
        (
            "crc k { width: 8, poly: 7, check: 0, data: rx, absorb: t.tick, restart: go }",
            "k",
            "crc.data",
            LogicalCheckStatus::Valid,
        ),
        (
            "crc k { width: 8, poly: 7, check: 0, data: t.tick, absorb: t.tick }",
            "k",
            "crc.data",
            LogicalCheckStatus::Invalid,
        ),
        (
            "crc k { width: 8, poly: 7, check: 0, data: rx, absorb: t.tick, restart: f }",
            "k",
            "crc.restart",
            LogicalCheckStatus::Invalid,
        ),
        (
            "crc k { width: 8, poly: 7, check: 0, link: t }",
            "k",
            "crc.link",
            LogicalCheckStatus::Invalid,
        ),
    ];
    for (line, consumer, port, expected) in cases {
        let source = format!("{BASE}{line}\n");
        let logical = graph(&source);
        let input = logical
            .inputs
            .iter()
            .find(|i| i.consumer == *consumer && i.port == *port)
            .unwrap_or_else(|| panic!("no {consumer} {port} input for `{line}`"));
        assert_eq!(
            input.check.status, *expected,
            "`{line}`: {:?}",
            input.check.reason
        );
        let others_valid = logical
            .inputs
            .iter()
            .all(|i| std::ptr::eq(i, input) || i.check.status == LogicalCheckStatus::Valid);
        assert!(others_valid, "`{line}` should vary only {consumer} {port}");
        let compiled = compile(&source);
        assert_eq!(
            compiled.is_ok(),
            *expected == LogicalCheckStatus::Valid,
            "`{line}`: elaborator says {:?}",
            compiled.err().map(|e| e.to_string())
        );
    }
}

#[test]
fn unresolved_sources_stay_unknown_and_formulas_report_source_counts() {
    let logical = graph(&format!(
        "{BASE}pin p: out = missing\nreg r = t.tick && go && f && f\n"
    ));
    let pin = logical.inputs.iter().find(|i| i.consumer == "p").unwrap();
    assert_eq!(pin.check.status, LogicalCheckStatus::Unknown);
    let reg = logical.inputs.iter().find(|i| i.consumer == "r").unwrap();
    assert_eq!(reg.check.status, LogicalCheckStatus::Valid);
    assert_eq!(
        reg.check.formula_sources,
        Some(LogicalFormulaSources {
            pulses: 2,
            levels: 1
        })
    );
}
