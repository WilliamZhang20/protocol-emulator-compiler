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