add_executable(ProtocolAssemblerTests
  EncoderTest.cpp
)

target_link_libraries(ProtocolAssemblerTests
  PRIVATE
    ProtocolAssemblerImpl
)

add_test(
  NAME ProtocolAssemblerTests
  COMMAND ProtocolAssemblerTests
)

add_executable(EmitProgram
  EmitProgram.cpp
)

target_link_libraries(EmitProgram
  PRIVATE
    ProtocolAssemblerImpl
)
