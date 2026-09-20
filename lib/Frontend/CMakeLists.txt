add_library(ProtocolFrontend
  AST.cpp
)

target_include_directories(ProtocolFrontend
  PUBLIC
  ${PROJECT_SOURCE_DIR}/include
)
