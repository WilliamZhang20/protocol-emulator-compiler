add_library(ProtocolAssemblerImpl
  Encoder.cpp
)

target_link_libraries(ProtocolAssemblerImpl
  PUBLIC
    ProtocolAssembler
)