// Hypothesis only: no RTL implements these UART operations or stream endpoints.
// Links are tile-to-tile reachability, not a port/selector implementation.
module {
  "spatial.target_graph"() {
    name = "uart-capability-hypothesis-v1",
    tiles = [
      {id = "host_tx", resource = "host", operations = ["stream_source"], width = 8 : i64, slots = 1 : i64, latency = 1 : i64, interval = 1 : i64},
      {id = "host_rx", resource = "host", operations = ["stream_sink"], width = 8 : i64, slots = 1 : i64, latency = 1 : i64, interval = 1 : i64},
      {id = "rx_pin", resource = "input", operations = ["input"], width = 1 : i64, slots = 1 : i64, latency = 1 : i64, interval = 1 : i64},
      {id = "edge", resource = "edge", operations = ["falling_edge"], width = 1 : i64, slots = 1 : i64, latency = 1 : i64, interval = 1 : i64},
      {id = "timer0", resource = "timer", operations = ["uart_timer"], width = 1 : i64, slots = 1 : i64, latency = 1 : i64, interval = 1 : i64, period_min = 1 : i64, period_max = 255 : i64, first_min = 1 : i64, first_max = 255 : i64},
      {id = "timer1", resource = "timer", operations = ["uart_timer"], width = 1 : i64, slots = 1 : i64, latency = 1 : i64, interval = 1 : i64, period_min = 1 : i64, period_max = 255 : i64, first_min = 1 : i64, first_max = 255 : i64},
      {id = "tx_shifter", resource = "shifter", operations = ["uart_tx_8n1"], width = 8 : i64, slots = 1 : i64, latency = 1 : i64, interval = 1 : i64},
      {id = "rx_shifter", resource = "shifter", operations = ["uart_rx_8n1"], width = 8 : i64, slots = 1 : i64, latency = 1 : i64, interval = 1 : i64},
      {id = "tx_pin", resource = "output", operations = ["output"], width = 1 : i64, slots = 1 : i64, latency = 1 : i64, interval = 1 : i64}
    ],
    links = [
      {from = "host_tx", to = "tx_shifter", width = 8 : i64, latency = 0 : i64},
      {from = "tx_shifter", to = "tx_pin", width = 1 : i64, latency = 0 : i64},
      {from = "tx_shifter", to = "timer0", width = 1 : i64, latency = 0 : i64},
      {from = "timer0", to = "tx_shifter", width = 1 : i64, latency = 0 : i64},
      {from = "rx_pin", to = "edge", width = 1 : i64, latency = 0 : i64},
      {from = "edge", to = "timer1", width = 1 : i64, latency = 0 : i64},
      {from = "rx_shifter", to = "timer1", width = 1 : i64, latency = 0 : i64},
      {from = "timer1", to = "rx_shifter", width = 1 : i64, latency = 0 : i64},
      {from = "edge", to = "rx_shifter", width = 1 : i64, latency = 0 : i64},
      {from = "rx_pin", to = "rx_shifter", width = 1 : i64, latency = 0 : i64},
      {from = "rx_shifter", to = "host_rx", width = 8 : i64, latency = 0 : i64}
    ]
  } : () -> ()
}
