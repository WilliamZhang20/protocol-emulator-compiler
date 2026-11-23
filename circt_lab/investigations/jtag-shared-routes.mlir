// Source-derived JTAG Boolean reachability hypothesis; no RTL or timing proof.
module {
  "spatial.fabric"() {
    name = "jtag-shared-selector-hypothesis", shift_width = 16 : i64,
    storage_words = 4 : i64, storage_word_bits = 16 : i64,
    control_states = 4 : i64, control_inputs = 2 : i64, route_cycles = 1 : i64,
    pad_max_output_hz_estimate = 66000000 : i64, pad_roundtrip_ns_estimate = 10 : i64,
    routes = [
      {sink = "tck.pin.drive", sources = ["clock.out"]},
      {sink = "tms.pin.drive", sources = ["route.out_shared"]},
      {sink = "tdi.pin.drive", sources = ["route.out_shared"]},
      {sink = "clock.timer.start", sources = ["data.busy"]},
      {sink = "clock.timer.stop", sources = ["data.done"]},
      {sink = "data.shifter.start", sources = ["go"]},
      {sink = "data.shifter.shift", sources = ["route.common_clock"]},
      {sink = "data.shifter.sample_data", sources = ["tdo"]},
      {sink = "data.shifter.sample_at", sources = ["clock.rise"]},
      {sink = "mode.shifter.start", sources = ["go"]},
      {sink = "mode.shifter.shift", sources = ["route.common_clock"]},
      {sink = "common_clock", sources = ["clock.fall"]},
      {sink = "out_shared", sources = ["mode.out", "data.out"]}
    ]
  } : () -> ()
}
