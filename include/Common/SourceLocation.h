#pragma once

#include <string>

namespace protocol {

struct SourceLocation {
  std::string file;
  unsigned line = 0;
  unsigned column = 0;
};

} // namespace protocol
