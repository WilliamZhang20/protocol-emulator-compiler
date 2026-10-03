#pragma once

#include <cstdint>
#include <string>

namespace protocol {

// Backend and validation inputs. Frontend syntax and AST do not depend on
// these limits, so a chip revision can change them without changing source.
struct TargetProfile {
  std::string name;
  unsigned gpioPins = 0;
  uint64_t engineClockHz = 0; // Supplied clock; 50 MHz in the reference setup.
  unsigned programBytes = 0;
  unsigned registerCount = 0;
  unsigned registerWidth = 0;
  unsigned actionLanes = 0;
  unsigned actionSlotsPerLane = 0;
  unsigned actionWordBits = 0;
  unsigned fifoDepth = 0;
  unsigned maxAtomicPinUpdates = 0;
  bool hasEdgeCapture = false;
  bool hasOpenDrain = false;
  bool hasAtomicPinUpdate = false;
};

// Describes the current RTL. A future chip revision can supply another
// profile without changing any protocol AST node.
inline TargetProfile currentTinyTapeoutTarget() {
  return {"protocol-emulator-cmos5l", 8, 50000000, 1024, 8, 16,
          2, 16, 32, 4, 2, true, true, true};
}

} // namespace protocol
