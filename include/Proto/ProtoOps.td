include "Proto/ProtoDialect.td"
include "mlir/IR/OpBase.td"

class Proto_Op<string mnemonic, list<Trait> traits = []> 
    : Op<Proto_Dialect, mnemonic, traits>;


def Proto_DriveOp : Proto_Op<"drive"> {
    let summary = "drive a pin";

    let arguments = (ins
        I32Attr:$pin
        I1Attr:$value
    );

    let assemblyFormat = [{
        $pin `,` $value attr-dict
    }];
}
