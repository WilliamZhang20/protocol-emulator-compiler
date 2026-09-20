#pragma once

#include "mlir/Bytecode/BytecodeOpInterface.h"
#include "mlir/IR/OpDefinition.h"
#include "mlir/IR/OpImplementation.h"
#include "Action/ActionDialect.h"

#define GET_OP_CLASSES
#include "Action/ActionOps.h.inc"
