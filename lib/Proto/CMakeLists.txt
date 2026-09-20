add_mlir_dialect_library(ProtoDialect
  ProtoDialect.cpp

  ADDITIONAL_HEADER_DIRS
  ${PROJECT_SOURCE_DIR}/include/Proto

  DEPENDS
  ProtoIncGen

  LINK_LIBS PUBLIC
  MLIRIR
)
