// A graph-mapper smoke test, not a complete source program or RTL image.
module {
  "spatial.workload_graph"() {
    nodes = [
      {id = "input", opcode = "input", resource = "input", inputs = [], outputs = [i1], duration = 1 : i64},
      {id = "output", opcode = "output", resource = "output", inputs = [i1], outputs = [], duration = 1 : i64}
    ],
    edges = [
      {from = "input", source_port = 0 : i64, to = "output", target_port = 0 : i64, mode = "data"}
    ]
  } : () -> ()
}
