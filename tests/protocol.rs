//! Source-to-actor protocol mapping checks.

use spatial::compile;

#[test]
fn protocol_transitions_map_to_an_actor_and_action_lines() {
    let build = compile(
        r#"
        pin request: in
        protocol control on request {
            transition 0 low -> 0
            transition 0 high -> 1 action 1
            transition 1 low -> 0 action 2
            transition 1 high -> 1
        }
        "#,
    )
    .expect("the two-state protocol fits an actor");

    let actor = build
        .resources
        .iter()
        .find(|resource| resource.name == "control")
        .expect("actor is included in the allocation report");
    assert_eq!(actor.kind, "protocol actor");
    assert_eq!(actor.index, 0);
    assert_eq!(
        actor.outputs,
        [("action1", 26), ("action2", 27), ("action3", 28)]
    );
}

#[test]
fn protocol_mapper_rejects_more_than_two_actors() {
    let error = compile(
        r#"
        protocol first on 0 { transition 0 low -> 0 }
        protocol second on 0 { transition 0 low -> 0 }
        protocol third on 0 { transition 0 low -> 0 }
        "#,
    )
    .expect_err("the 8x4 fabric has two actors");
    assert!(error.to_string().contains("only 2 protocol actors"));
}

#[test]
fn protocol_mapper_rejects_duplicate_input_rows() {
    let error = compile(
        r#"
        protocol duplicate on 0 {
            transition 0 high -> 0
            transition 0 high -> 1
        }
        "#,
    )
    .expect_err("each state/input pair has one transition");
    assert!(error.to_string().contains("two transitions for input high"));
}

#[test]
fn actor_placement_follows_the_shifter_using_its_action() {
    let build = compile(
        r#"
        shifter channel0 { start: second.action1 }
        shifter channel1 { start: first.action1 }
        protocol first on 0 { transition 0 low -> 0 }
        protocol second on 0 { transition 0 low -> 0 }
        "#,
    )
    .expect("both protocol graphs fit the actor array");

    let actor = |name: &str| {
        build
            .resources
            .iter()
            .find(|resource| resource.name == name)
            .expect("protocol resource is reported")
            .index
    };
    assert_eq!(actor("first"), 1);
    assert_eq!(actor("second"), 0);
    let first = build
        .protocol_mappings
        .iter()
        .find(|mapping| mapping.name == "first")
        .expect("mapping report contains the graph");
    assert_eq!(first.local_channel_connections, 1);
    assert_eq!(first.placement_candidates_evaluated, 2);
}
