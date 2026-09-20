#include "Proto/ProtoDialect.h"
#include "Proto/ProtoOps.h"

#include "mlir/IR/DiaelctImplementation.h"

using namespace mlir;

#include "Proto/ProtoDialect.cpp.inc"

void proto::ProtoDialect::initialize() {
    addOperations<
#define GET_OP_LIST
#include "Proto/ProtoOps.cpp.inc"
        >();
}
