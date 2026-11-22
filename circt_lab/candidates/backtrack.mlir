// A's first route occupies relay X; B needs X. A must be rerouted through Y.
module {
  "spatial.fabric"() {
    name = "routing-test", shift_width = 16 : i64,
    storage_words = 4 : i64, storage_word_bits = 16 : i64,
    control_states = 4 : i64, control_inputs = 2 : i64, route_cycles = 1 : i64,
    pad_max_output_hz_estimate = 66000000 : i64, pad_roundtrip_ns_estimate = 10 : i64,
    routes = [
      {sink = "a", sources = ["route.x", "route.y"]},
      {sink = "b", sources = ["route.x"]},
      {sink = "x", sources = ["producer_a", "producer_b"]},
      {sink = "y", sources = ["producer_a"]}
    ]
  } : () -> ()
}
