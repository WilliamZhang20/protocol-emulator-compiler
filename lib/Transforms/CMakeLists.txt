# Add lowering and legalization passes here.

# Write the Proto to Action lowering pass in:
#   lib/Transforms/ProtoToAction.cpp

if(EXISTS "\${CMAKE_CURRENT_SOURCE_DIR}/ProtoToAction.cpp")
  add_mlir_library(ProtoToAction
    ProtoToAction.cpp
    LINK_LIBS PUBLIC
    MLIRIR
    ProtoDialect
    ActionDialect
  )
endif()
