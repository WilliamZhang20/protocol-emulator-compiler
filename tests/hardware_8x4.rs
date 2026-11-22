//! Check the compiler's physical register and signal map against the 8x4 RTL.
use spatial::compile;

fn has_config(image: &[u8], address: u8, value: u16) -> bool {
    image
        .windows(3)
        .any(|bytes| bytes == [address, value as u8, (value >> 8) as u8])
}

#[test]
fn pin_edges_use_configurable_cells_and_a_distinct_line_each() {
    let build = compile("pin a: in pin b: in reg both = a.rise || b.fall").unwrap();
    assert!(build.usage.contains(&("edge cells", 2, 8)));
    assert!(has_config(&build.image, 0x80, 1 << 3));
    assert!(has_config(&build.image, 0x81, 1 | 1 << 4));
    // Logic cell 0 reads edge lines 16 and 17.
    assert!(has_config(&build.image, 0x41, 16 | 17 << 6));
}

#[test]
fn edge_cell_capacity_is_checked_after_pad_assignment() {
    let source = "pin a: in pin b: in pin c: in pin d: in pin e: in \
        reg a_edges = a.rise || a.fall reg b_edges = b.rise || b.fall \
        reg c_edges = c.rise || c.fall reg d_edges = d.rise || d.fall \
        reg e_edges = e.rise || e.fall";
    let error = compile(source).unwrap_err();
    assert!(format!("{error:?}").contains("only 8 edge cells"));
}

#[test]
fn timer_resync_and_hold_use_current_field_positions() {
    let build =
        compile("pin a: in timer t { every: 8 cycles, run: 1, resync: a.rise, hold: !a }").unwrap();
    assert!(has_config(
        &build.image,
        0x03,
        16 | 1 << 6 | 1 << 12 | 1 << 13
    ));
}

#[test]
fn second_crc_uses_its_own_registers_and_signal_lines() {
    let build = compile("crc first { width: 8, poly: 7, check: 0 } crc second { width: 8, poly: 29, check: 0 } reg good = second.ok").unwrap();
    assert!(build.usage.contains(&("crc", 2, 2)));
    assert!(has_config(&build.image, 0x28, 7));
    assert!(has_config(&build.image, 0x88, 29));
    assert!(has_config(&build.image, 0x41, 51));
}

#[test]
fn shifter_coder_and_linked_crc_encode_both_channels() {
    let build = compile("shifter a { manchester, crc_tail_bits: 32, crc_skip_bits: 64 } shifter b { stuff_after: 5, stuff_ones, nrzi, code_received } crc first { width: 32, poly: 4, check: 0, link: a } crc second { width: 16, poly: 7, check: 0, link: b }").unwrap();
    assert!(has_config(&build.image, 0x36, 1 << 15 | 32 << 9));
    assert!(has_config(&build.image, 0x38, 64 << 6));
    assert!(has_config(&build.image, 0x37, 5 | 1 << 3 | 1 << 4 | 1 << 5));
    assert!(has_config(&build.image, 0x2e, 31 | 1 << 5 | 1 << 6));
    assert!(has_config(
        &build.image,
        0x8e,
        15 | 1 << 5 | 1 << 6 | 1 << 7
    ));
}

#[test]
fn coded_ethernet_frees_logic_and_counters() {
    let build = compile(include_str!("../examples/ethernet_tx_coded.spt")).unwrap();
    assert!(build.usage.contains(&("logic cells", 3, 8)));
    assert!(build.usage.contains(&("counters", 0, 2)));
    assert!(build.image.len() < 1024);
}

#[test]
fn selector_graph_decodes_named_pin_and_timer_connections() {
    let build = compile(include_str!("../examples/usb.spt")).unwrap();
    let pin = build
        .signal_connections
        .iter()
        .find(|edge| edge.destination == "dp" && edge.input == "pin.drive")
        .unwrap();
    assert_eq!(pin.source.as_deref(), Some("d_plus"));
    assert_eq!(pin.line, Some(62));

    let timer = build
        .signal_connections
        .iter()
        .find(|edge| edge.destination == "bit" && edge.input == "timer.start")
        .unwrap();
    assert_eq!(timer.source.as_deref(), Some("bits"));
    assert_eq!(timer.line, Some(46));
}

#[test]
fn linked_crc_is_a_direct_non_line_connection() {
    let build = compile(include_str!("../examples/ethernet_tx_coded.spt")).unwrap();
    let link = build
        .signal_connections
        .iter()
        .find(|edge| edge.destination == "fcs" && edge.input == "crc.link_data")
        .unwrap();
    assert_eq!(link.source.as_deref(), Some("bits"));
    assert_eq!(link.line, None);
    assert_eq!(link.transport, "direct_link");
    let activity =
        spatial::estimate_direct_activity(&build.resources, &build.signal_connections, &[100]);
    let searched = activity
        .iter()
        .find(|estimate| estimate.topology == "graph_search_2")
        .unwrap();
    assert_eq!(searched.cross_cluster_events, Some(0));
}

#[test]
fn plan_exports_byte_pipeline_stage_tiles() {
    let build = compile(include_str!("../examples/byte_framing.spt")).unwrap();
    let send = &build.plan["streams"]["send"]["byte_stages"];
    let recv = &build.plan["streams"]["recv"]["byte_stages"];
    assert_eq!(send[0]["kind"], "checksum");
    assert_eq!(send[0]["tile"], 0);
    assert_eq!(send[1]["kind"], "escape");
    assert_eq!(send[1]["tile"], 1);
    assert_eq!(recv[0]["kind"], "unescape");
    assert_eq!(recv[0]["tile"], 6);
    assert_eq!(recv[1]["kind"], "checksum");
    assert_eq!(recv[1]["tile"], 7);
    let byte_links = build
        .signal_connections
        .iter()
        .filter(|edge| edge.transport == "byte_link")
        .collect::<Vec<_>>();
    assert_eq!(byte_links.len(), 4);
    assert!(byte_links.iter().all(|edge| edge.line.is_none()));
    assert!(byte_links.iter().any(|edge| {
        edge.source.as_deref() == Some("send.stage1") && edge.destination == "sender"
    }));
    assert!(byte_links.iter().any(|edge| {
        edge.source.as_deref() == Some("receiver") && edge.destination == "recv.stage0"
    }));
    let activity = spatial::estimate_byte_activity(
        &build.resources,
        &build.signal_connections,
        &[18, 9, 9, 18],
    );
    let searched = activity
        .iter()
        .find(|estimate| estimate.topology == "graph_search_2")
        .unwrap();
    let stages = activity
        .iter()
        .find(|estimate| estimate.topology == "stage_hierarchy")
        .unwrap();
    assert_eq!(searched.cross_cluster_transfers, Some(0));
    assert!(stages.cross_cluster_transfers.unwrap() > 0);
}

#[test]
fn topology_report_keeps_physical_locality_unknown_and_models_alternatives() {
    let build = compile(include_str!("../examples/usb.spt")).unwrap();
    let flat = build
        .topology_estimates
        .iter()
        .find(|estimate| estimate.topology == "flat64")
        .unwrap();
    assert_eq!(flat.cross_cluster_connections, None);

    let channel = build
        .topology_estimates
        .iter()
        .find(|estimate| estimate.topology == "channel_split")
        .unwrap();
    let stages = build
        .topology_estimates
        .iter()
        .find(|estimate| estimate.topology == "stage_hierarchy")
        .unwrap();
    assert_eq!(channel.cross_cluster_connections, Some(16));
    assert_eq!(stages.cross_cluster_connections, Some(25));
    let searched = build
        .topology_estimates
        .iter()
        .find(|estimate| estimate.topology == "graph_search_2")
        .unwrap();
    assert!(
        searched.cross_cluster_connections.unwrap() < channel.cross_cluster_connections.unwrap()
    );
}

#[test]
fn parallel_static_sends_compile_with_equal_or_unequal_lengths() {
    let source = r#"
        const A = [0x10, 0x11, 0x12, 0x13]
        const B = [0x20, 0x21, 0x22, 0x23]
        flag start_a = 0
        shifter a { frame: [0, data(8), 1], idle: 1, start: start_a }
        shifter b { frame: [0, data(8), 1], idle: 1 }
        export fn send_both() {
            parallel {
                a.send(A)
                b.send(B)
            }
        }
    "#;
    let build = compile(source).unwrap();
    assert!(build.image.len() < 1024);
    assert_eq!(build.plan["calls"]["channel"], 0);
    // Shifter 0 shares its TX FIFO with call dispatch. Its boot configuration
    // must leave auto-pull off; the controller enables it only after pulling
    // the call terminator, then disables it again before dispatch resumes.
    assert!(has_config(&build.image, 0x13, 0x36));
    assert!(has_config(&build.image, 0x13, 0x836));

    let unequal = source.replace(
        "const B = [0x20, 0x21, 0x22, 0x23]",
        "const B = [0x20, 0x21, 0x22]",
    );
    let result = compile(&unequal).unwrap();
    let transfer = &result.memory_layout.stored_transfers[0];
    assert_eq!(transfer.table, [0x10, 0x20, 0x11, 0x21, 0x12, 0x22, 0x13]);
    assert_eq!(transfer.phases[1].writes, [(0, false), (1, true)]);
    assert_eq!(transfer.phases[3].writes, [(0, true)]);
}
