// Single fixed workload; later work must lower a real .spt program to this IR.
module {
  %received = "spatial.spi"() {
    target = "spi-fabric-v1", width = 8 : i64, half_period = 8 : i64,
    clock_hz = 50000000 : i64, tx_value = 165 : i64,
    storage_words_required = 2 : i64,
    connections = {
      start = "control.start", run = "shift.busy", rise = "timer.rise",
      fall = "timer.fall", input = "pin.miso", mosi = "shift.out",
      sck = "timer.level", cs = "control.not_selected"
    }
  } : () -> i8
}
