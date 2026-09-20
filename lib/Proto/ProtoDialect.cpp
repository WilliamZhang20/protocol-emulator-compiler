#include "Proto/ProtoDialect.h"
#include "Proto/ProtoOps.h"

#include "mlir/IR/Builders.h"
#include "mlir/IR/DialectImplementation.h"
#include "mlir/IR/ImplicitLocOpBuilder.h"
#include "mlir/IR/OpImplementation.h"

using namespace mlir;

#include "Proto/ProtoDialect.cpp.inc"

void proto::ProtoDialect::initialize() {
    addOperations<
#define GET_OP_LIST
#include "Proto/ProtoOps.cpp.inc"
        >();
}

#define GET_OP_CLASSES
#include "Proto/ProtoOps.cpp.inc"
