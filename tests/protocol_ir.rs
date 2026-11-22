//! Logical protocol IR and its relationship to the actor encoding.
use spatial::protocol::{ProtocolGraph, Transition, map_actor};
use spatial::{LogicalSignal, compile, extract_logical_protocols};

#[test]
fn extracts_unallocated_state_machines_even_when_target_cannot_fit() {
    let src = "const LAST = 300 protocol a on signal { transition 0 high -> LAST action 4 } protocol b on 0 {} protocol c on 0 {}";
    let logical = extract_logical_protocols(src).unwrap();
    assert_eq!(logical.len(), 3);
    assert_eq!(logical[0].states, 301);
    assert_eq!(
        logical[0].inputs,
        [LogicalSignal::Resource {
            name: "signal".into(),
            output: None
        }]
    );
    assert_eq!(logical[0].step(0, &[true]).unwrap().state, 300);
    assert_eq!(logical[0].step(0, &[false]).unwrap().state, 0);
    assert!(compile(src).is_err());
}

#[test]
fn ir_retains_joint_inputs_and_implicit_holds() {
    let src = "pin a: in pin b: in protocol p on a, b { transition 0 high low -> 1 action 2 transition 1 low high -> 0 action 1 }";
    let ir = extract_logical_protocols(src).unwrap().remove(0);
    assert_eq!(ir.step(0, &[true, false]).unwrap().action, Some(2));
    assert_eq!(ir.step(1, &[false, true]).unwrap().state, 0);
    assert_eq!(ir.step(1, &[true, true]).unwrap().state, 1);
    assert!(ir.step(0, &[true]).is_err());
    assert_eq!(compile(src).unwrap().logical_protocols[0], ir);
}

fn compare_encoding(source: &str, alph: usize) {
    let logical = extract_logical_protocols(source).unwrap().remove(0);
    let graph = ProtocolGraph {
        name: logical.name.clone(),
        input_line: 4,
        input_line2: (logical.inputs.len() == 2).then_some(5),
        states: logical.states,
        initial_state: logical.initial_state,
        transitions: logical
            .transitions
            .iter()
            .map(|row| Transition {
                from: row.from,
                to: row.to,
                input_high: row.inputs[0],
                input2_high: row.inputs.get(1).copied().unwrap_or(false),
                action: row.action,
            })
            .collect(),
    };
    let mapping = map_actor(&graph, 0).unwrap();
    let select = mapping
        .writes
        .iter()
        .find(|(address, _)| *address == 0xa0)
        .unwrap()
        .1;
    assert_eq!((select >> 12) != 0, mapping.wide_state);
    // Exhaustive bounded input sequences cover implicit holds and repeated
    // transitions, including the state minimizer's remapping.
    for sequence in 0..alph.pow(6) {
        let mut input_sequence = sequence;
        let mut logical_state = 0;
        let mut physical_state = 0usize;
        for _ in 0..6 {
            let input = input_sequence % alph;
            input_sequence /= alph;
            let bits = [input & 1 != 0, input & 2 != 0];
            let expected = logical
                .step(logical_state, &bits[..logical.inputs.len()])
                .unwrap();
            logical_state = expected.state;
            let row = if mapping.wide_state {
                physical_state * 2 + (input & 1)
            } else {
                physical_state * 4 + input
            };
            let word = mapping
                .writes
                .iter()
                .find(|(address, _)| *address == 0x90 + (row / 2) as u8)
                .unwrap()
                .1;
            let width = if mapping.wide_state { 6 } else { 5 };
            let data = (word >> ((row % 2) * width)) & ((1 << width) - 1);
            let state_bits = if mapping.wide_state { 3 } else { 2 };
            physical_state = (data as usize) & ((1 << state_bits) - 1);
            let action_bits = data >> state_bits;
            let actual_action = if action_bits == 0 {
                None
            } else {
                Some(action_bits.trailing_zeros() as usize + 1)
            };
            assert_eq!(
                actual_action, expected.action,
                "sequence {sequence}, input {input}"
            );
        }
    }
}

#[test]
fn actor_configuration_preserves_logical_ir_behavior() {
    compare_encoding(
        "protocol p on a, b { transition 0 high low -> 1 action 1 transition 1 low high -> 0 action 2 transition 1 high high -> 1 action 3 }",
        4,
    );
    compare_encoding(
        "protocol p on a { transition 0 high -> 1 action 1 transition 1 high -> 2 transition 2 high -> 3 action 2 transition 3 high -> 4 transition 4 high -> 0 action 3 }",
        2,
    );
}

#[test]
fn candidate_assessment_accounts_for_reachability_equivalence_and_unknown_latency() {
    use spatial::ProtocolCandidate;
    let src = "protocol p on request { transition 0 high -> 2 transition 2 high -> 2 transition 1 high -> 1 transition 1000 high -> 1000 action 99 }";
    let ir = extract_logical_protocols(src).unwrap().remove(0);
    let analysis = ir.analyze().unwrap();
    assert_eq!(analysis.reachable_states, [0, 2]);
    assert_eq!(analysis.unreachable_state_count, 999);
    assert_eq!(analysis.equivalence_classes, [vec![0, 2]]);
    let mut candidate = ProtocolCandidate {
        max_inputs: 1,
        max_states: 1,
        max_action: 0,
        response_cycles: None,
        response_deadline_cycles: Some(2),
    };
    let result = ir.assess(&candidate).unwrap();
    assert!(result.capacity_fits); // unreachable action 99 is irrelevant
    assert_eq!(result.response_deadline_met, None);
    candidate.response_cycles = Some(3);
    assert_eq!(
        ir.assess(&candidate).unwrap().response_deadline_met,
        Some(false)
    );
    candidate.max_states = 0;
    assert!(!ir.assess(&candidate).unwrap().capacity_fits);
}

#[test]
fn analysis_rejects_inconsistent_public_ir() {
    let mut ir = extract_logical_protocols("protocol p on a { transition 0 high -> 1 }")
        .unwrap()
        .remove(0);
    ir.transitions[0].to = 100;
    assert!(ir.analyze().is_err());
    assert!(ir.step(0, &[true]).is_err());
}
