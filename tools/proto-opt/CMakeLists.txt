get_property(dialect_libs GLOBAL PROPERTY MLIR_DIALECT_LIBS)

set(LIBS
  ${dialect_libs}
  MLIROptLib
  ProtoDialect
)

add_llvm_executable(proto-opt proto-opt.cpp)

llvm_update_compile_flags(proto-opt)
target_link_libraries(proto-opt PRIVATE ${LIBS})

mlir_check_all_link_libraries(proto-opt)
