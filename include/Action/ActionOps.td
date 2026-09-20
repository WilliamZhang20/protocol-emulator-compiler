include "Action/ActionDialect.td"
include "mlir/IR/OpBase.td"

class Action_Op<string mnemonic, list<Trait> traits = []>
    : Op<Action_Dialect, mnemonic, traits>;

// Pin and target fields are three bits in the RTL. Attribute ranges are
// checked when instructions are encoded.
def Action_NopOp : Action_Op<"nop"> {
  let summary = "Advance to the next action slot";
  let assemblyFormat = "attr-dict";
}

def Action_GpioOp : Action_Op<"gpio"> {
  let summary = "Update output and output-enable state of one pin";
  let arguments = (ins I32Attr:$pin, I1Attr:$out_we, I1Attr:$out,
                       I1Attr:$oe_we, I1Attr:$oe);
  let assemblyFormat = "$pin `,` $out_we `,` $out `,` $oe_we `,` $oe attr-dict";
}

def Action_SampleOp : Action_Op<"sample"> {
  let summary = "Sample a timed pin into the action result register";
  let arguments = (ins I32Attr:$pin, I1Attr:$shift, I1Attr:$shift_left);
  let results = (outs I1:$value);
  let assemblyFormat = "$pin `,` $shift `,` $shift_left attr-dict `:` type($value)";
}

def Action_ShiftOp : Action_Op<"shift"> {
  let summary = "Shift one bit through the shared 16-bit shift register";
  let arguments = (ins I32Attr:$tx_pin, I32Attr:$rx_pin, I1Attr:$input,
                       I1Attr:$msb_first, I1Attr:$duplex, I1Attr:$open_drain);
  let assemblyFormat = "$tx_pin `,` $rx_pin `,` $input `,` $msb_first `,` $duplex `,` $open_drain attr-dict";
}

def Action_CountOp : Action_Op<"count"> {
  let summary = "Load, increment, decrement, or decrement-and-branch the counter";
  let arguments = (ins I32Attr:$mode, I32Attr:$immediate,
                       I32Attr:$target, I1Attr:$finish);
  let assemblyFormat = "$mode `,` $immediate `,` $target `,` $finish attr-dict";
}

def Action_CrcOp : Action_Op<"crc"> {
  let summary = "Feed the low shift-register byte to the CRC engine";
  let assemblyFormat = "attr-dict";
}

def Action_NextOp : Action_Op<"next"> {
  let summary = "Conditionally branch to an action table slot";
  let arguments = (ins I32Attr:$condition, I32Attr:$target);
  let assemblyFormat = "$condition `,` $target attr-dict";
}

def Action_DelayOp : Action_Op<"delay"> {
  let summary = "Wait for a count and optionally a sampled pin";
  let arguments = (ins I32Attr:$cycles, I1Attr:$wait_pin, I32Attr:$pin);
  let assemblyFormat = "$cycles `,` $wait_pin `,` $pin attr-dict";
}

def Action_RepeatOp : Action_Op<"repeat"> {
  let summary = "Unconditionally jump to an action table slot";
  let arguments = (ins I32Attr:$target);
  let assemblyFormat = "$target attr-dict";
}

def Action_DoneOp : Action_Op<"done"> {
  let summary = "Finish the action program or restart its next pass";
  let assemblyFormat = "attr-dict";
}

def Action_PairOp : Action_Op<"pair"> {
  let summary = "Decode the sampled state of a two-pin pair";
  let arguments = (ins I32Attr:$pin_a, I32Attr:$pin_b, I1Attr:$swap);
  let assemblyFormat = "$pin_a `,` $pin_b `,` $swap attr-dict";
}

def Action_PullOp : Action_Op<"pull"> {
  let summary = "Load the shift register from the TX FIFO";
  let arguments = (ins I32Attr:$bits, I1Attr:$msb_first);
  let assemblyFormat = "$bits `,` $msb_first attr-dict";
}

def Action_PushOp : Action_Op<"push"> {
  let summary = "Push a shifted result byte to the RX FIFO";
  let arguments = (ins I1Attr:$high_byte, I32Attr:$right_shift);
  let assemblyFormat = "$high_byte `,` $right_shift attr-dict";
}
