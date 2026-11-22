//! Source-to-actor protocol mapping checks.

use spatial::compile;

#[test]
fn programs_without_actors_do_not_require_actor_abi() {
    let build = compile(include_str!("../examples/uart.spt")).expect("UART compiles");
    assert!(build.plan["actor_abi"].is_null());
}

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
    assert_eq!(build.plan["actor_abi"], 2);

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
fn two_input_protocol_actor_maps_joint_conditions() {
    let build = compile(
        r#"
        pin tick: in
        pin ack: in
        protocol control on tick, ack {
            transition 0 low low -> 0
            transition 0 high low -> 0 action 1
            transition 0 low high -> 0
            transition 0 high high -> 0 action 2
        }
        "#,
    )
    .expect("two selected conditions map into the actor");

    let mapping = &build.protocol_mappings[0];
    assert_eq!(mapping.input_line2, Some(33));
    assert_eq!(mapping.implementation, "spatial actor");
}

#[test]
fn two_input_timer_protocol_compiles_to_one_spatial_actor() {
    let build = compile(include_str!("../examples/protocol_control_two_input.spt"))
        .expect("two timer conditions fit one spatial actor");
    assert_eq!(
        build.tests,
        ["two-input actor handles both timer conditions"]
    );
    assert_eq!(build.protocol_mappings[0].input_line2, Some(41));
    assert_eq!(build.protocol_mappings[0].mapped_states, 2);
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
fn compiler_minimizes_protocol_states_before_actor_allocation() {
    let build = compile(
        r#"
        pin request: in
        protocol reduced on request {
            transition 0 low -> 2
            transition 0 high -> 1 action 1
            transition 1 low -> 1
            transition 1 high -> 1
            transition 2 low -> 2
            transition 2 high -> 2
            transition 3 low -> 3
            transition 3 high -> 3
            transition 4 low -> 4
            transition 4 high -> 4
        }
        "#,
    )
    .expect("unreachable and behaviorally equal states are removed");
    let mapping = &build.protocol_mappings[0];
    assert_eq!(mapping.source_states, 5);
    assert_eq!(mapping.mapped_states, 2);
    assert_eq!(mapping.states_reduced, 3);
}

#[test]
fn compiler_maps_five_state_one_input_graph_to_extended_actor() {
    let build = compile(
        r#"
        timer request { every: 16 cycles, pulse, run: 1 }
        protocol handshake on request.tick {
            transition 0 low -> 0
            transition 0 high -> 1
            transition 1 low -> 1
            transition 1 high -> 2
            transition 2 low -> 2
            transition 2 high -> 3
            transition 3 low -> 3
            transition 3 high -> 4 action 1
            transition 4 low -> 4
            transition 4 high -> 4
        }
        "#,
    )
    .expect("five states fit the one-input extended actor mode");
    let mapping = &build.protocol_mappings[0];
    assert_eq!(mapping.implementation, "spatial actor");
    assert_eq!(mapping.actor, Some(0));
    assert!(mapping.wide_state);
    assert_eq!(mapping.source_states, 5);
    assert_eq!(mapping.mapped_states, 5);
    assert_eq!(mapping.actor_pipeline_cycles, Some(1));
    assert_eq!(mapping.controller_service_cycles, None);
    assert!(build.protocol_placement_search.candidates[0].feasible);
}

#[test]
fn compiler_rejects_controller_fallback_for_a_level_input_graph() {
    let error = compile(
        r#"
        pin request: in
        protocol chain on request {
            transition 0 low -> 0
            transition 0 high -> 1 action 1
            transition 1 low -> 1
            transition 1 high -> 2
            transition 2 low -> 2
            transition 2 high -> 3
            transition 3 low -> 3
            transition 3 high -> 4
            transition 4 low -> 4
            transition 4 high -> 5
            transition 5 low -> 5
            transition 5 high -> 6
            transition 6 low -> 6
            transition 6 high -> 7
            transition 7 low -> 7
            transition 7 high -> 8 action 1
            transition 8 low -> 8
            transition 8 high -> 8 action 2
        }
        "#,
    )
    .expect_err("nine observable states exceed both actor modes");
    assert!(
        error
            .to_string()
            .contains("not eligible for the PX8 controller backend")
    );
}

#[test]
fn controller_fallback_rejects_its_own_fire_lines_as_inputs() {
    let error = compile(
        r#"
        event request
        protocol chain on request {
            transition 0 low -> 0
            transition 0 high -> 1 action 1
            transition 1 low -> 1
            transition 1 high -> 2
            transition 2 low -> 2
            transition 2 high -> 3
            transition 3 low -> 3
            transition 3 high -> 4
            transition 4 low -> 4
            transition 4 high -> 5
            transition 5 low -> 5
            transition 5 high -> 6
            transition 6 low -> 6
            transition 6 high -> 7
            transition 7 low -> 7
            transition 7 high -> 8 action 1
            transition 8 low -> 8
            transition 8 high -> 8 action 2
        }
        "#,
    )
    .expect_err("controller FIRE outputs must not feed its own input");
    assert!(
        error.to_string().contains("timer tick pulse on P4..P25"),
        "unexpected diagnostic: {error}"
    );
}

#[test]
fn compiler_selects_px8_controller_for_an_oversized_pulse_graph() {
    let build = compile(
        r#"
        timer request { every: 16 cycles, pulse, run: 1 }
        protocol chain on request.tick {
            transition 0 low -> 0
            transition 0 high -> 1 action 1
            transition 1 low -> 1
            transition 1 high -> 2
            transition 2 low -> 2
            transition 2 high -> 3
            transition 3 low -> 3
            transition 3 high -> 4
            transition 4 low -> 4
            transition 4 high -> 5
            transition 5 low -> 5
            transition 5 high -> 6
            transition 6 low -> 6
            transition 6 high -> 7
            transition 7 low -> 7
            transition 7 high -> 8 action 1
            transition 8 low -> 8
            transition 8 high -> 8 action 2
        }
        "#,
    )
    .expect("the controller backend handles pulse-driven graphs over eight states");
    let mapping = &build.protocol_mappings[0];
    assert_eq!(mapping.implementation, "PX8 controller");
    assert_eq!(mapping.actor, None);
    assert_eq!(mapping.mapped_states, 9);
    assert_eq!(mapping.input_line, 4);
    assert_eq!(mapping.controller_service_cycles, Some(10));
    assert_eq!(mapping.minimum_input_interval_cycles, Some(16));
    assert!(
        build.protocol_placement_search.candidates[0..2]
            .iter()
            .all(|candidate| !candidate.feasible)
    );
    assert_eq!(build.protocol_placement_search.selected, Some(2));
    let resource = build
        .resources
        .iter()
        .find(|resource| resource.name == "chain")
        .unwrap();
    assert_eq!(resource.kind, "PX8 protocol controller");
    assert_eq!(resource.outputs, [("action1", 2), ("action2", 3)]);
}

#[test]
fn compiler_evaluates_controller_alongside_a_fitting_actor() {
    let build = compile(
        r#"
        timer request { every: 16 cycles, pulse, run: 1 }
        protocol handshake on request.tick {
            transition 0 low -> 0
            transition 0 high -> 1 action 1
            transition 1 low -> 1
            transition 1 high -> 1
        }
        "#,
    )
    .expect("the two-state graph maps to an actor");
    let mapping = &build.protocol_mappings[0];
    assert_eq!(mapping.implementation, "spatial actor");
    assert_eq!(mapping.actor_pipeline_cycles, Some(1));
    assert_eq!(mapping.placement_candidates_evaluated, 3);
    assert_eq!(build.protocol_placement_search.selected, Some(0));
    let controller = &build.protocol_placement_search.candidates[2];
    assert_eq!(controller.implementation, "PX8 controller");
    assert!(controller.feasible);
    assert_eq!(controller.parallel_actors, 0);
    assert_eq!(controller.controller_service_cycles, Some(10));
    assert_eq!(controller.minimum_input_interval_cycles, Some(16));
}

#[test]
fn controller_fallback_rejects_pulses_faster_than_its_service_rate() {
    let error = compile(
        r#"
        timer request { every: 10 cycles, pulse, run: 1 }
        protocol chain on request.tick {
            transition 0 low -> 0
            transition 0 high -> 1 action 1
            transition 1 low -> 1
            transition 1 high -> 2
            transition 2 low -> 2
            transition 2 high -> 3
            transition 3 low -> 3
            transition 3 high -> 4
            transition 4 low -> 4
            transition 4 high -> 5
            transition 5 low -> 5
            transition 5 high -> 6
            transition 6 low -> 6
            transition 6 high -> 7
            transition 7 low -> 7
            transition 7 high -> 8 action 1
            transition 8 low -> 8
            transition 8 high -> 8 action 2
        }
        "#,
    )
    .expect_err("a new pulse must not arrive before the controller can re-arm its wait");
    assert!(
        error
            .to_string()
            .contains("minimum input interval greater than 10")
    );
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
    assert_eq!(build.protocol_placement_search.selected, Some(1));
    assert_eq!(
        build
            .protocol_placement_search
            .candidates
            .iter()
            .map(|candidate| candidate.local_channel_connections)
            .collect::<Vec<_>>(),
        [0, 2]
    );
    assert_eq!(
        build
            .protocol_placement_search
            .candidates
            .iter()
            .map(|candidate| candidate.parallel_actors)
            .collect::<Vec<_>>(),
        [2, 2]
    );
}
