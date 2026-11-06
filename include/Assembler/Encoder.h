#pragma once

#include "Assembler/AST.h"

#include <cstdint>
#include <vector>

namespace protocol {
namespace assembler {
        // just an API for encoding one instruction (instruction -> chip machine code)
        std::vector<uint8_t> encodeInstruction(const Instruction& instruction);
}
}