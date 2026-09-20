#include "Action/ActionDialect.h"
#include "Action/ActionOps.h"

#include "mlir/IR/Builders.h"
#include "mlir/IR/DialectImplementation.h"
#include "mlir/IR/ImplicitLocOpBuilder.h"
#include "mlir/IR/OpImplementation.h"

using namespace mlir;

#include "Action/ActionDialect.cpp.inc"

void action::ActionDialect::initialize() {
  addOperations<
#define GET_OP_LIST
#include "Action/ActionOps.cpp.inc"
      >();
}

#define GET_OP_CLASSES
#include "Action/ActionOps.cpp.inc"
