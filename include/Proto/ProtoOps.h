#pragma once

#include "mlir/Bytecode/BytecodeOpInterface.h"
#include "mlir/IR/OpDefinition.h"
#include "mlir/IR/OpImplementation.h"

#include "Proto/ProtoDialect.h"

#define GET_OP_CLASSES

#include "Proto/ProtoOps.h.inc"


