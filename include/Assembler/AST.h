#pragma once

#include "Common/SourceLocation.h"

#include <cstdint>
#include <string>
#include <variant>
#include <vector>

namespace protocol {
namespace assembler {

// Parsed assembly retains symbolic operands. Address resolution and byte
// encoding belong to the assembler implementation.
struct Immediate {
  int64_t value = 0;
};

struct Register {
  unsigned index = 0;
};

struct Symbol {
  std::string name;
};

using Operand = std::variant<Immediate, Register, Symbol>;

struct Label {
  SourceLocation location;
  std::string name;
};

struct Instruction {
  SourceLocation location;
  std::string mnemonic;
  std::vector<Operand> operands;
};

using Statement = std::variant<Label, Instruction>;

struct Program {
  std::string file;
  std::vector<Statement> statements;
};

} // namespace assembler
} // namespace protocol
