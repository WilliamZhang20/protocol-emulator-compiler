// Experimental unregistered spatial ops: mapper supplies the semantic verifier.
// This is a measured RTL prototype description, not a general chip schema.
module {
  "spatial.fabric"() {
    name = "spi-fabric-v1",
    shift_width = 16 : i64,
    storage_words = 4 : i64,
    storage_word_bits = 16 : i64,
    control_states = 4 : i64,
    control_inputs = 2 : i64,
    route_cycles = 1 : i64,
    pad_max_output_hz_estimate = 66000000 : i64,
    pad_roundtrip_ns_estimate = 10 : i64,
    routes = [
      {sink = "start", sources = ["control.start", "zero", "trigger", "timer.rise"]},
      {sink = "run", sources = ["shift.busy", "control.selected", "zero", "one"]},
      {sink = "rise", sources = ["timer.rise", "timer.fall", "zero", "trigger"]},
      {sink = "fall", sources = ["timer.fall", "timer.rise", "zero", "trigger"]},
      {sink = "input", sources = ["pin.miso", "zero", "one", "shift.out"]},
      {sink = "mosi", sources = ["shift.out", "timer.level", "zero", "one"]},
      {sink = "sck", sources = ["timer.level", "shift.out", "zero", "one"]},
      {sink = "cs", sources = ["control.not_selected", "control.selected", "zero", "one"]}
    ]
  } : () -> ()
}
