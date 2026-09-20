# Generate the dialect declaration/definition.



set(LLVM_TARGET_DEFINITIONS ProtoDialect.td)



mlir_tablegen(

  ProtoDialect.h.inc

  -gen-dialect-decls

  -dialect=proto

)



mlir_tablegen(

  ProtoDialect.cpp.inc

  -gen-dialect-defs

  -dialect=proto

)



# Generate operation declaration/definition.



set(LLVM_TARGET_DEFINITIONS ProtoOps.td)



mlir_tablegen(

  ProtoOps.h.inc

  -gen-op-decls

)



mlir_tablegen(

  ProtoOps.cpp.inc

  -gen-op-defs

)



# One target representing all generated files.

add_public_tablegen_target(ProtoIncGen)
