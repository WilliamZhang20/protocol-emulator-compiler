add_library(ProtocolAssembler
  Encoder.cpp
)

target_include_directories(ProtocolAssembler
  PUBLIC
  ${PROJECT_SOURCE_DIR}/include
)