include "Proto/ProtoDialect.td"
include "mlir/IR/OpBase.td"

class Proto_Op<string mnemonic, list<Trait> traits = []> 
    : Op<Proto_Dialect, mnemonic, traits>;


def Proto_DriveOp : Proto_Op<"drive"> {
    let summary = "drive a pin";

    let arguments = (ins
        I32Attr:$pin,
        I1Attr:$value
    );

    let assemblyFormat = [{
        $pin `,` $value attr-dict
    }];
}

def Proto_DelayOp : Proto_Op<"delay"> {
    let summary = "Delay for a number of ASIC cyles";

    let arguments = (ins
        I32Attr:$cycles
    );

    let asemblyFormat = [{
        $cycles: attr-dict
    }];
}

def Proto_SampleOp : Proto_Op<"sample"> {
    let summary = "sample a protocol pin";

    let arguments = (ins
       I32Attr:$pin
    );

    let assemblyFormat = [{
       $pin attr-dict `:` type($value)
    }];
}
