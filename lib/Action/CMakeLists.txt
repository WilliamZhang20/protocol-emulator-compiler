add_mlir_dialect_library(ActionDialect
  ActionDialect.cpp
  DEPENDS ActionIncGen
  LINK_LIBS PUBLIC MLIRIR
)
