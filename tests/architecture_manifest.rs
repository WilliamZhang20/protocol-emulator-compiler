//! The exported graph retains compilation facts and declares its limitations.

#[test]
fn manifest_contains_the_same_allocations_and_selector_edges_as_compile() {
    let source = include_str!("../examples/uart.spt");
    let build = spatial::compile(source).unwrap();
    let manifest = build.architecture_manifest();
    assert_eq!(manifest["graph_complete"], false);
    assert_eq!(manifest["hardware_revision"], spatial::HARDWARE_REVISION);
    assert_eq!(
        manifest["resources"].as_array().unwrap().len(),
        build.resources.len()
    );
    assert_eq!(
        manifest["edges"].as_array().unwrap().len(),
        build.signal_connections.len()
    );
    assert_eq!(
        manifest["unresolved_producer_edges"].as_u64().unwrap() as usize,
        build
            .signal_connections
            .iter()
            .filter(|edge| edge.source.is_none())
            .count()
    );
    assert!(manifest["limitations"].as_array().unwrap().len() >= 3);
}
