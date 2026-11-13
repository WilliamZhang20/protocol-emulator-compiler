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
