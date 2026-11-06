// Encodes a fixed straight-line program with the assembler and prints it as
// one hex byte per line. The cocotb stage loads these bytes into the chip, so
// the encoder is exercised end to end against the RTL.
#include "Assembler/Encoder.h"

#include <cstdio>
#include <string>
#include <vector>

using namespace protocol::assembler;

namespace {
Instruction make(const std::string& mnemonic, std::vector<Operand> operands = {}) {
  Instruction instruction;
  instruction.mnemonic = mnemonic;
  instruction.operands = std::move(operands);
  return instruction;
}
} // namespace

int main() {
  // pin 4: drive high, hold 20 cycles, drive low, hold 20 cycles, halt
  const std::vector<Instruction> program = {
      make("gpio_oe", {Immediate{4}, Immediate{1}}),
      make("gpio_write", {Immediate{4}, Immediate{1}}),
      make("wait16", {Immediate{20}}),
      make("gpio_write", {Immediate{4}, Immediate{0}}),
      make("wait16", {Immediate{20}}),
      make("halt"),
  };
  for (const Instruction& instruction : program)
    for (uint8_t byte : encodeInstruction(instruction))
      std::printf("%02x\n", byte);
  return 0;
}
