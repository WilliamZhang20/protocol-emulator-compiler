//! Context resource accounting and diagnostics.
use spatial::compile;

fn reject(source: &str, message: &str) {
    let error = compile(source).expect_err("invalid context must fail");
    assert!(format!("{error:?}").contains(message), "{error:?}");
}

#[test]
fn contexts_share_physical_resources_and_inherit_defaults() {
    let build = compile(include_str!("../examples/contexts.spt")).unwrap();
    assert!(build.usage.contains(&("shifters", 1, 2)));
    assert!(build.usage.contains(&("timers", 1, 4)));
    assert_eq!(build.tests.len(), 3);
}

#[test]
fn rejects_third_context() {
    reject(
        "flag phase contexts(phase) { context 2 {} }",
        "must be 0 or 1",
    );
}

#[test]
fn rejects_pulse_selector() {
    reject(
        "event go contexts(go) { context 0 {} context 1 {} }",
        "must be a level",
    );
}

#[test]
fn requires_grouped_syntax_and_both_contexts() {
    reject("context 1 {}", "contexts(level)");
    reject("flag a contexts a", "expected `(`");
    reject("flag a contexts(a) { context 1 {} }", "include both");
    reject(
        "flag a contexts(a) { context 0 {} context 1 {} } contexts(a) { context 0 {} context 1 {} }",
        "only one",
    );
}

#[test]
fn rejects_context_one_only_resources_and_pad_changes() {
    reject(
        "flag a contexts(a) { context 0 {} context 1 { timer t { every: 4 cycles } } }",
        "declare the shared tile",
    );
    reject(
        "flag a pin p: out = 0 contexts(a) { context 0 {} context 1 { pin p @ 2: out = 1 } }",
        "pad assignment",
    );
}

#[test]
fn rejects_shared_crc_and_duplicate_bank() {
    reject(
        "flag a contexts(a) { context 0 { crc c { width: 16, poly: 0x8005, check: 0 } } context 1 { crc c { width: 8 } } }",
        "only existing timers",
    );
    reject(
        "flag a contexts(a) { context 1 {} context 1 {} }",
        "already defined",
    );
}

#[test]
fn rejects_duplicate_overrides_and_fields() {
    reject(
        "flag a contexts(a) { context 0 { pin p: out = 0 } context 1 { pin p: out = 1 pin p: out = 0 } }",
        "overridden twice",
    );
    reject(
        "flag a contexts(a) { context 0 { timer t { every: 8 cycles, run: 1 } } context 1 { timer t { every: 4 cycles, every: 6 cycles } } }",
        "duplicate context field",
    );
}

#[test]
fn rejects_unsupported_declarations_in_context() {
    reject(
        "flag a contexts(a) { context 0 {} context 1 { flag b } }",
        "only overrides existing",
    );
}

#[test]
fn context_order_does_not_allocate_extra_hardware() {
    let build = compile(
        "flag a = 1 contexts(a) { context 1 { pin p: out = 1 } context 0 { pin p: out = 0 } }",
    )
    .unwrap();
    assert!(build.usage.contains(&("pins", 1, 8)));
}

#[test]
fn logic_cells_can_change_formula_without_reallocating_or_losing_mode() {
    let build = compile(
        "flag phase = 0 event tick pin a: in \
         contexts(phase) { \
           context 0 { reg selected init 1 on tick = a } \
           context 1 { reg selected = !a } \
         } \
         pin out: out = selected",
    )
    .unwrap();
    assert!(build.usage.contains(&("logic cells", 1, 8)));
    let has_write = |address: u8, value: u16| {
        build
            .image
            .windows(3)
            .any(|bytes| bytes == [address, value as u8, (value >> 8) as u8])
    };
    assert!(has_write(0x40, 0xaaaa));
    assert!(has_write(0x40, 0x5555));
    // The context-1 formula inherits the shared start value and update pulse.
    assert_eq!(
        build
            .image
            .windows(3)
            .filter(|bytes| bytes[0] == 0x43 && bytes[1] == (2 | 1 << 5) && bytes[2] == 0)
            .count(),
        3
    );
}

#[test]
fn context_one_cannot_introduce_a_logic_cell() {
    reject(
        "flag phase contexts(phase) { context 0 {} context 1 { reg missing = 1 } }",
        "declare the shared reg",
    );
}

#[test]
fn logic_cell_declared_in_context_zero_can_follow_context_one_in_source() {
    let build = compile(
        "flag phase pin a: in contexts(phase) { \
         context 1 { reg selected = !a } \
         context 0 { reg selected = a } } \
         pin out: out = selected",
    )
    .unwrap();
    assert!(build.usage.contains(&("logic cells", 1, 8)));
}

#[test]
fn logic_context_uart_example_fits_two_cells() {
    let build = compile(include_str!("../examples/contexts_logic.spt")).unwrap();
    assert!(build.usage.contains(&("logic cells", 2, 8)));
    assert_eq!(build.tests.len(), 2);
}

#[test]
fn validates_flag_initializers() {
    reject("flag a = 2", "0 or 1");
    compile("flag a = 0, b = 1").unwrap();
}
