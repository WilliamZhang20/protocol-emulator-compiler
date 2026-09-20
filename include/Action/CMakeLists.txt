set(LLVM_TARGET_DEFINITIONS ActionDialect.td)
mlir_tablegen(ActionDialect.h.inc -gen-dialect-decls -dialect=action)
mlir_tablegen(ActionDialect.cpp.inc -gen-dialect-defs -dialect=action)

set(LLVM_TARGET_DEFINITIONS ActionOps.td)
mlir_tablegen(ActionOps.h.inc -gen-op-decls)
mlir_tablegen(ActionOps.cpp.inc -gen-op-defs)

add_public_tablegen_target(ActionIncGen)
