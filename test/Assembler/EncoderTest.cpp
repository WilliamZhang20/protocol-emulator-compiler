#include "Assembler/Encoder.h"

#include <cassert>
#include <cstdint>
#include <iostream>
#include <stdexcept>
#include <vector>

using namespace protocol::assembler;

namespace {

// interface for making instructions
Instruction makeInstruction(
  const std::string& mnemonic,
  std::vector<Operand> operands = {}) {
  Instruction instruction;
  instruction.mnemonic = mnemonic;
  instruction.operands = std::move(operands);
  return instruction;
}

// test the nop (as instruction should be just 00)
void testNop() {
  auto bytes = encodeInstruction(makeInstruction("nop"));
  assert(bytes == std::vector<uint8_t>{0x00});
}

// test the halt (should be just 01)
void testHalt() {
  auto bytes = encodeInstruction(makeInstruction("halt"));
  assert(bytes == std::vector<uint8_t>{0x01});
}

// test the wait16 (should be just 10 _____)
void testWait16() {
  auto bytes = encodeInstruction(
				makeInstruction("wait16", {Immediate{423}}));

  assert(bytes == (std::vector<uint8_t>{0x10, 0xA7, 0x01}));
}

// test the gpio write
void testGpioWrite() {
  auto bytes = encodeInstruction(
		makeInstruction("gpio_write", {Immediate{3}, Immediate{1}}));

  assert(bytes == std::vector<uint8_t>{0x2B});
}

// test the gpio oe
void testGpioOe() {
  auto bytes = encodeInstruction(
          makeInstruction("gpio_oe",{Immediate{3}, Immediate{1}}));

  assert(bytes == std::vector<uint8_t>{0x3B});
}

// test the shift out
void testShiftOut() {
  auto bytes = encodeInstruction(
          makeInstruction("shift_out", {Immediate{5}}));

  assert(bytes == std::vector<uint8_t>{0x55});
}

// test the shift in
void testShiftIn() {
  auto bytes = encodeInstruction(
				makeInstruction("shift_in", {Immediate{5}}));

  assert(bytes == std::vector<uint8_t>{0x65});
}

// test the set instruction
void testSet() {
  auto bytes = encodeInstruction(
          makeInstruction("set",{Register{2}, Immediate{42}}));

  assert(bytes == (std::vector<uint8_t>{0xAA, 0x02, 0x2A}));
}

// test the mov instruction
void testMov() {
  auto bytes = encodeInstruction(
          makeInstruction("mov",{Register{3}, Register{5}}));

  assert(bytes == (std::vector<uint8_t>{0xAB, 0x2B}));
}

// test the add instruction
void testAdd() {
  auto bytes = encodeInstruction(
          makeInstruction("add", {Register{3}, Register{5}}));

  assert(bytes == (std::vector<uint8_t>{0xAC, 0x00, 0x2B}));
}

// test an invalid register
void testInvalidRegister() {
  bool threw = false;

  try {
    encodeInstruction( // 0 to 7
			makeInstruction("mov",{Register{8}, Register{0}}));
  } catch (const std::out_of_range&) {
    threw = true;
  }

  assert(threw);
}

// test an invalid pin (only 0 to 7)
void testInvalidPin() {
  bool threw = false;

  try {
    encodeInstruction(
			makeInstruction("shift_out", {Immediate{8}}));
  } catch (const std::out_of_range&) {
    threw = true;
  }

  assert(threw);
}

// test giving an instruction the wrong number of operands
void testWrongOperandCount() {
  bool threw = false;

  try {
    encodeInstruction( // this doesn't make sense, nop is solo
        makeInstruction("nop", {Immediate{1}}));
  } catch (const std::invalid_argument&) {
    threw = true;
  }

  assert(threw);
}

// test the sub instruction
void testSub() {
  auto bytes = encodeInstruction(
    makeInstruction("sub", {Register{3}, Register{5}}));

  assert(bytes == (std::vector<uint8_t>{0xAC, 0x01, 0x2B}));
}

// test the and instruction
void testAnd() {
  auto bytes = encodeInstruction(
    makeInstruction("and", {Register{3}, Register{5}}));

  assert(bytes == (std::vector<uint8_t>{0xAC, 0x02, 0x2B}));
}

// test the or instruction
void testOr() {
  auto bytes = encodeInstruction(
    makeInstruction("or", {Register{3}, Register{5}}));

  assert(bytes == (std::vector<uint8_t>{0xAC, 0x03, 0x2B}));
}

// test the xor instruction
void testXor() {
  auto bytes = encodeInstruction(
    makeInstruction("xor", {Register{3}, Register{5}}));

  assert(bytes == (std::vector<uint8_t>{0xAC, 0x04, 0x2B}));
}

// test the shl instruction
void testShl() {
  auto bytes = encodeInstruction(
    makeInstruction("shl", {Register{3}, Register{5}}));

  assert(bytes == (std::vector<uint8_t>{0xAC, 0x05, 0x2B}));
}

// test the shr instruction
void testShr() {
  auto bytes = encodeInstruction(
    makeInstruction("shr", {Register{3}, Register{5}}));

  assert(bytes == (std::vector<uint8_t>{0xAC, 0x06, 0x2B}));
}

// test a negative wait16 value
void testNegativeWait16() {
  bool threw = false;

  try {
    encodeInstruction(
      makeInstruction("wait16", {Immediate{-1}}));
  } catch (const std::out_of_range&) {
    threw = true;
  }

  assert(threw);
}

// test a wait16 value that's too large
void testLargeWait16() {
  bool threw = false;

  try {
    encodeInstruction(
      makeInstruction("wait16", {Immediate{70000}}));
  } catch (const std::out_of_range&) {
    threw = true;
  }

  assert(threw);
}

} //namespace

int main() {

  // run through all the tests
  testNop();
  testHalt();
  testWait16();
  testGpioWrite();
  testGpioOe();
  testShiftOut();
  testShiftIn();
  testSet();
  testMov();

	// ALU type tests
  testAdd();
  testSub();
  testAnd();
  testOr();
  testXor();
  testShl();
  testShr();

  testInvalidRegister();
  testInvalidPin();
  testWrongOperandCount();
  testNegativeWait16();
  testLargeWait16();

  std::cout << "All assembler encoder tests passed.\n";
  return 0;
}