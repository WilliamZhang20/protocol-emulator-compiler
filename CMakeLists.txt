cmake_minimum_required(VERSION 3.20)



project(protocol_emulator_compiler LANGUAGES C CXX)



set(CMAKE_CXX_STANDARD 17)

set(CMAKE_CXX_STANDARD_REQUIRED ON)



find_package(MLIR REQUIRED CONFIG)

find_package(LLVM REQUIRED CONFIG)



message(STATUS "Using MLIRConfig.cmake in: ${MLIR_DIR}")

message(STATUS "Using LLVMConfig.cmake in: ${LLVM_DIR}")



list(APPEND CMAKE_MODULE_PATH

  "${MLIR_CMAKE_DIR}"

  "${LLVM_CMAKE_DIR}"

)



include(TableGen)

include(AddLLVM)

include(AddMLIR)

include(HandleLLVMOptions)



include_directories(${LLVM_INCLUDE_DIRS})

include_directories(${MLIR_INCLUDE_DIRS})



# Your source headers.

include_directories(${PROJECT_SOURCE_DIR}/include)



# Generated *.inc files land under build/include.

include_directories(${PROJECT_BINARY_DIR}/include)



add_definitions(${LLVM_DEFINITIONS})



set(CMAKE_RUNTIME_OUTPUT_DIRECTORY ${PROJECT_BINARY_DIR}/bin)

set(CMAKE_LIBRARY_OUTPUT_DIRECTORY ${PROJECT_BINARY_DIR}/lib)



add_subdirectory(include)

add_subdirectory(lib)

add_subdirectory(tools)
