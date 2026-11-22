//! Causal memory experiments and invalid input handling.
use spatial::MemoryExperiment;

fn experiment() -> MemoryExperiment {
    serde_json::from_str(include_str!("fixtures/memory_experiment.json")).unwrap()
}

#[test]
fn independent_requests_benefit_from_banks_but_dependencies_remain() {
    let result = experiment().evaluate().unwrap();
    assert_eq!(result[0].completion_cycle, 6);
    assert_eq!(result[0].total_queue_cycles, 4);
    assert_eq!(result[0].deadlines_missed, 1);
    assert_eq!(result[1].completion_cycle, 4);
    assert_eq!(result[1].total_queue_cycles, 0);
    assert_eq!(result[1].deadlines_missed, 0);
    assert_eq!(result[1].accesses[2].ready, 2);
}

#[test]
fn pipelining_and_bank_conflicts_are_distinct() {
    let mut exp = experiment();
    exp.candidates.truncate(1);
    exp.candidates[0].latency = 4;
    exp.candidates[0].initiation_interval = 1;
    let result = exp.evaluate().unwrap();
    assert_eq!(result[0].accesses[1].start, 1);
    assert_eq!(result[0].accesses[2].ready, 4);
    assert_eq!(result[0].accesses[2].completion, 8);
}

#[test]
fn rejects_cycles_unknown_dependencies_and_cross_stripe_accesses() {
    let mut exp = experiment();
    exp.requests[0].depends_on.push("c".into());
    assert!(exp.evaluate().unwrap_err().contains("cycle"));
    exp.requests[0].depends_on = vec!["missing".into()];
    assert!(exp.evaluate().unwrap_err().contains("unknown"));
    exp.requests[0].depends_on.clear();
    exp.requests[0].bytes = 2;
    assert!(exp.evaluate().unwrap_err().contains("crosses a stripe"));
}

#[test]
fn rejects_duplicate_ids_overflow_and_unknown_fields() {
    let mut exp = experiment();
    exp.requests[1].id = "a".into();
    assert!(exp.evaluate().unwrap_err().contains("duplicate"));
    exp.requests[1].id = "b".into();
    exp.requests[0].address = u64::MAX;
    exp.requests[0].bytes = 2;
    assert!(exp.evaluate().unwrap_err().contains("overflow"));
    assert!(
        serde_json::from_str::<MemoryExperiment>(
            r#"{"requests":[],"candidates":[],"unexpected":true}"#
        )
        .is_err()
    );
}
