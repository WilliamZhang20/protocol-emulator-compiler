include "mlir/IR/DialectBase.td"

def Action_Dialect : Dialect {
  let name = "action";
  let cppNamespace = "::action";
  let summary = "Instructions for the protocol emulator action engine";
  let description = [{
    Decoded primary 16-bit action instructions. A table slot may also have
    a parallel lane, which is represented separately.
  }];
}
