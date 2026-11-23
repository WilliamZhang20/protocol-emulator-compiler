// Abstract resource/connection model of the tested slice. It does not encode
// selector conflicts, link occupancy, pin timing, or the host program.
module {
  "spatial.target_graph"() {
    name = "memory-spi-abstract-v1", pad_max_output_hz_estimate = 66000000 : i64,
    tiles = [
      {id = "host_trigger", resource = "input", operations = ["input"], width = 1 : i64, slots = 1 : i64, latency = 1 : i64, interval = 1 : i64},
      {id = "miso_pin", resource = "input", operations = ["input"], width = 1 : i64, slots = 1 : i64, latency = 1 : i64, interval = 1 : i64},
      {id = "bank1", resource = "memory", operations = ["read", "write"], width = 8 : i64, depth = 1024 : i64, slots = 1 : i64, latency = 1 : i64, interval = 1 : i64},
      {id = "bank0", resource = "memory", operations = ["read", "write"], width = 8 : i64, depth = 1024 : i64, slots = 1 : i64, latency = 1 : i64, interval = 1 : i64},
      {id = "adapter", resource = "adapter", operations = ["pack_big_endian_low_bits", "unpack_big_endian_low_bits"], width = 16 : i64, slots = 1 : i64, latency = 1 : i64, interval = 1 : i64},
      {id = "control", resource = "control", operations = ["protocol"], width = 1 : i64, slots = 1 : i64, latency = 1 : i64, interval = 1 : i64, states = 4 : i64, control_inputs = 2 : i64},
      {id = "timer", resource = "timer", operations = ["timer"], width = 1 : i64, slots = 1 : i64, latency = 1 : i64, interval = 1 : i64, period_min = 8 : i64, period_max = 255 : i64},
      {id = "shift", resource = "shifter", operations = ["shift_msb_first"], width = 16 : i64, slots = 1 : i64, latency = 1 : i64, interval = 1 : i64},
      {id = "cs_route", resource = "invert", operations = ["invert"], width = 1 : i64, slots = 1 : i64, latency = 1 : i64, interval = 1 : i64},
      {id = "mosi_pin", resource = "output", operations = ["output"], width = 1 : i64, slots = 1 : i64, latency = 1 : i64, interval = 1 : i64},
      {id = "sck_pin", resource = "output", operations = ["output"], width = 1 : i64, slots = 1 : i64, latency = 1 : i64, interval = 1 : i64},
      {id = "cs_pin", resource = "output", operations = ["output"], width = 1 : i64, slots = 1 : i64, latency = 1 : i64, interval = 1 : i64}
    ],
    links = [
      {from = "bank0", to = "adapter", width = 8 : i64, latency = 0 : i64},
      {from = "bank1", to = "adapter", width = 8 : i64, latency = 0 : i64},
      {from = "adapter", to = "bank0", width = 8 : i64, latency = 0 : i64},
      {from = "adapter", to = "bank1", width = 8 : i64, latency = 0 : i64},
      {from = "adapter", to = "control", width = 1 : i64, latency = 0 : i64},
      {from = "adapter", to = "shift", width = 16 : i64, latency = 0 : i64},
      {from = "control", to = "shift", width = 1 : i64, latency = 1 : i64},
      {from = "timer", to = "shift", width = 1 : i64, latency = 1 : i64},
      {from = "shift", to = "timer", width = 1 : i64, latency = 1 : i64},
      {from = "shift", to = "control", width = 1 : i64, latency = 0 : i64},
      {from = "miso_pin", to = "shift", width = 1 : i64, latency = 3 : i64},
      {from = "shift", to = "adapter", width = 16 : i64, latency = 0 : i64},
      {from = "shift", to = "mosi_pin", width = 1 : i64, latency = 2 : i64},
      {from = "timer", to = "sck_pin", width = 1 : i64, latency = 2 : i64},
      {from = "shift", to = "cs_route", width = 1 : i64, latency = 0 : i64},
      {from = "cs_route", to = "cs_pin", width = 1 : i64, latency = 1 : i64}
    ]
  } : () -> ()
}
