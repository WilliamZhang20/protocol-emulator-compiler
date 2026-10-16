#include "Assembler/Encoder.h"

#include <stdexcept>
#include <string>

namespace protocol {
namespace assembler {

namespace {

// makes sure that operand count makes sense
// otherwise, throw an error
void requireOperandCount(const Instruction& instruction, std::size_t expected) {
  if (instruction.operands.size() != expected) {
    throw std::invalid_argument(instruction.mnemonic + " expects " + std::to_string(expected) + " operand(s)");
  }
}

// get the operand, which is immediate (if we expect an
// immediate operand but the casting fails, we don't have an immediate)
int64_t getImmediate(const Operand& operand) {

  // get immediate operand (if not immediate will be null)
  const auto* immediate = std::get_if<Immediate>(&operand);

  if (!immediate) {
    throw std::invalid_argument("expected immediate operand");
  } else {
    return immediate->value;
  }
}

// get the operand, which is register operator r0-r7 (if we expect an
// immediate operand but the casting fails, we don't have an immediate)
unsigned getRegister(const Operand& operand) {

  // get register operand (if not immediate will be null)
  const auto* reg = std::get_if<Register>(&operand);
  
  if (!reg) {
    throw std::invalid_argument("expected register operand");
  } else if (reg->index > 7) { // chip only has 8 general purpose
    throw std::out_of_range("register must be r0-r7");
  }

  return reg->index;
}

// ensure we have a unsigned 8 bit integer and return it as a uint8
uint8_t requireU8(int64_t value) {
  if (value < 0 || value > 0xFF) {
    throw std::out_of_range("value does not fit in 8 bits");
  }

  return static_cast<uint8_t>(value);
}

// ensure we have a unsigned 16 bit integer and return it as a uint16
uint16_t requireU16(int64_t value) {
  if (value < 0 || value > 0xFFFF) {
    throw std::out_of_range("value does not fit in 16 bits");
  }

  return static_cast<uint16_t>(value);
}

// ensure we have a valid pin and return it
unsigned requirePin(int64_t value) {
  if (value < 0 || value > 7) {
    throw std::out_of_range("pin must be in range 0-7");
  }

  return static_cast<unsigned>(value);
}

// concatenate two registers into [5:3] and [2:0]
uint8_t packRegisters(unsigned rd, unsigned rs) {
  return static_cast<uint8_t>((rs << 3) | rd);
}

} // namespace

// this is the main instruction encoder function
std::vector<uint8_t> encodeInstruction(const Instruction& instruction) {
  
  // get the mnemonic for the instruction  
  const std::string& mnemonic = instruction.mnemonic;

  if (mnemonic == "nop") { // NOP -----------------------------
    requireOperandCount(instruction, 0);
    return {0x00}; // nop is just 00
  } else if (mnemonic == "halt") { // HALT
    requireOperandCount(instruction, 0);
    return {0x01}; // halt is just 01
  } else if (mnemonic == "wait16") { // WAIT16 N
    requireOperandCount(instruction, 1); // ensure we have only one operand (number of cycles blocked)

    const uint16_t value = requireU16(getImmediate(instruction.operands[0]));
    const uint8_t low = static_cast<uint8_t>(value & 0xFF);           // encoding into
    const uint8_t high = static_cast<uint8_t>((value >> 8) & 0xFF);   // little endian

    return {0x10, low, high}; // wait16 0x10
  } else if (mnemonic == "gpio_write") { // GPIO_WRITE PIN, VALUE
    requireOperandCount(instruction, 2); // ensure we only have two operaands

    const unsigned pin = requirePin(getImmediate(instruction.operands[0]));
    const int64_t value = getImmediate(instruction.operands[1]);

    if (value != 0 && value != 1) { // only low or high
      throw std::out_of_range("gpio value must be 0 or 1");
    }

    // GPIO write is 0x20
    return { static_cast<uint8_t>(0x20 | (static_cast<unsigned>(value) << 3) | pin) }; 
  } else if (mnemonic == "gpio_oe") { // GPIO_OE PIN, ENABLE
    requireOperandCount(instruction, 2);  // ensure we only have two operaands

    const unsigned pin = requirePin(getImmediate(instruction.operands[0]));
    const int64_t enable = getImmediate(instruction.operands[1]);

    if (enable != 0 && enable != 1) { // only low or high
      throw std::out_of_range("gpio enable must be 0 or 1");
    }

    // GPIO OE is 0x30
    return { static_cast<uint8_t>(0x30 | (static_cast<unsigned>(enable) << 3) | pin) };
  } else if (mnemonic == "tx_load") { // TX_LOAD
    requireOperandCount(instruction, 0); // ensure no operands

    return {0x40}; // tx_load is 0x40
  } else if (mnemonic == "shift_out") { // SHIFT_OUT PIN
    requireOperandCount(instruction, 1); // ensure that we have only one operand

    const unsigned pin = requirePin(getImmediate(instruction.operands[0]));

    return { static_cast<uint8_t>(0x50 | pin) }; // SHIFT OUT is 0x50
  } else if (mnemonic == "shift_in") {  // SHIFT_IN PIN
    requireOperandCount(instruction, 1); // ensure that we have only one operand

    const unsigned pin = requirePin(getImmediate(instruction.operands[0]));

    return { static_cast<uint8_t>(0x60 | pin) }; // SHIFT IN is 0x60
  } else if (mnemonic == "rx_push") { // RX_PUSH
    requireOperandCount(instruction, 0); // ensure that we have no operands

    return {0x70}; // RX_PUSH is 0x70
  } else if (mnemonic == "set") { // SET Rd, IMM8
    requireOperandCount(instruction, 2); // ensure that we have 2 operands only

    const unsigned rd = getRegister(instruction.operands[0]);
    const uint8_t immediate = requireU8(getImmediate(instruction.operands[1]));

    // the opcode is 0xAA for this
    return { 0xAA, static_cast<uint8_t>(rd), immediate };
  } else if (mnemonic == "mov") { // MOV Rd, Rs
    requireOperandCount(instruction, 2); // ensure that we have 2 operands only

    const unsigned rd = getRegister(instruction.operands[0]);
    const unsigned rs = getRegister(instruction.operands[1]);

    // mov is 0xAB
    return { 0xAB, packRegisters(rd, rs) };
  }

  // IF NONE OF THE ABOVE MATCH, WE CHECK POSSIBLE ALU OPERATIONS
  // struct kinda excessive but it works... and looks cleaner than exhaustion
  const struct {
    const char* mnemonic;
    uint8_t opcode;
  } aluOperations[] = {
      {"add", 0},
      {"sub", 1},
      {"and", 2},
      {"or",  3},
      {"xor", 4},
      {"shl", 5},
      {"shr", 6},
  };

  // cycle through all ALU operations
  for (const auto& alu : aluOperations) {
    if (mnemonic == alu.mnemonic) {
      requireOperandCount(instruction, 2); // ALU operations need 2 operands

      const unsigned rd = getRegister(instruction.operands[0]);
      const unsigned rs = getRegister(instruction.operands[1]);

      // ALU instructions are 0xAC, followed by opcodes and the registers
      return { 0xAC, alu.opcode, packRegisters(rd, rs) }; 
    }
  }

  // i'll add more later, but for now if we have no match it's not supported
  throw std::invalid_argument("unsupported instruction: " + mnemonic);
}

} // namespace assembler
} // namespace protocol