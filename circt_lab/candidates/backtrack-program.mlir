// Synthetic selector demands; this is not a tested physical SPI graph.
module {
  %received = "spatial.spi"() {
    target = "routing-test", width = 8 : i64, half_period = 8 : i64,
    clock_hz = 50000000 : i64, tx_value = 165 : i64, storage_words_required = 2 : i64,
    connections = {a = "producer_a", b = "producer_b"}
  } : () -> i8
}
