// Native CIRCT FSM representation of the prototype control-state graph.
// This captures state transitions only. The mapper consumes this verified
// CIRCT FSM, but currently supplies SPI-specific registered actions itself.
module {
  fsm.machine @control(%trigger: i1, %done: i1) attributes {initialState = "IDLE"} {
    fsm.state @IDLE transitions {
      fsm.transition @START guard { fsm.return %trigger }
    }
    fsm.state @START transitions {
      fsm.transition @TRANSFER
    }
    fsm.state @TRANSFER transitions {
      fsm.transition @FINISHED guard { fsm.return %done }
    }
    fsm.state @FINISHED transitions {
      fsm.transition @START guard { fsm.return %trigger }
    }
  }
}
