include "Proto/ProtoDialect.td"
include "mlir/IR/OpBase.td"

class Proto_Op<string mnemonic, list<Trait> traits = []> 
    : Op<Proto_Dialect, mnemonic, traits>;


def Proto_DriveOp : Proto_Op<"drive"> {
    let summary = "drive a pin";

    let arguments = (ins
        StrAttr:$pin,
        I1:$value
    );

    let assemblyFormat = "$pin `,` $value attr-dict `:` type($value)";
}

def Proto_OutputEnableOp : Proto_Op<"output_enable"> {
    let summary = "set the output enable of a protocol pin";
    let arguments = (ins StrAttr:$pin, I1:$enabled);
    let assemblyFormat = "$pin `,` $enabled attr-dict `:` type($enabled)";
}

def Proto_DelayOp : Proto_Op<"delay"> {
    let summary = "Delay for a physical duration";

    let arguments = (ins
        I64:$duration_fs
    );

    let assemblyFormat = "$duration_fs attr-dict `:` type($duration_fs)";
}

def Proto_WaitHighOp : Proto_Op<"wait_high"> {
    let summary = "wait until a protocol pin is high";
    let arguments = (ins StrAttr:$pin);
    let assemblyFormat = "$pin attr-dict";
}

def Proto_WaitEdgeOp : Proto_Op<"wait_edge"> {
    let summary = "wait for a rising or falling edge on a protocol pin";
    let arguments = (ins StrAttr:$pin, StrAttr:$edge);
    let assemblyFormat = "$pin `,` $edge attr-dict";
}

def Proto_SampleOp : Proto_Op<"sample"> {
    let summary = "sample a protocol pin";

    let arguments = (ins
       StrAttr:$pin
    );

    let results = (outs I1:$value);

    let assemblyFormat = [{
       $pin attr-dict `:` type($value)
    }];
}
