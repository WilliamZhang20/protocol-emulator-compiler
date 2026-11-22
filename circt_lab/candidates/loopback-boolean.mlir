// Hypothetical logical-name wiring for loopback; not implemented RTL.
module {
  "spatial.fabric"() {
    name = "loopback-logical", shift_width = 16 : i64,
    storage_words = 4 : i64, storage_word_bits = 16 : i64,
    control_states = 4 : i64, control_inputs = 2 : i64, route_cycles = 1 : i64,
    pad_max_output_hz_estimate = 66000000 : i64, pad_roundtrip_ns_estimate = 10 : i64,
    routes = [
      {sink = "out_wire.pin.drive", sources = ["route.relay_out", "zero", "one"]},
      {sink = "send_clock.timer.start", sources = ["sender.busy", "zero", "one"]},
      {sink = "send_clock.timer.stop", sources = ["sender.done", "zero", "one"]},
      {sink = "sender.shifter.shift", sources = ["send_clock.tick", "zero", "one"]},
      {sink = "read_clock.timer.start", sources = ["in_wire.fall", "zero", "one"]},
      {sink = "read_clock.timer.stop", sources = ["receiver.done", "zero", "one"]},
      {sink = "receiver.shifter.start", sources = ["in_wire.fall", "zero", "one"]},
      {sink = "receiver.shifter.shift", sources = ["read_clock.tick", "zero", "one"]},
      {sink = "receiver.shifter.sample_data", sources = ["in_wire", "zero", "one"]},
      {sink = "receiver.shifter.sample_at", sources = ["read_clock.tick", "zero", "one"]},
      {sink = "relay_out", sources = ["sender.out", "zero", "one"]}
    ]
  } : () -> ()
}
