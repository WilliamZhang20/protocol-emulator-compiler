#pragma once

#include "Common/SourceLocation.h"

#include <cstdint>
#include <memory>
#include <optional>
#include <string>
#include <utility>
#include <vector>

namespace protocol {
namespace ast {

using SourceLocation = protocol::SourceLocation;

struct Type {
  enum class Kind { Void, Bool, Byte, Int, Bits, Pin };
  Kind kind = Kind::Void;
  // Used by Bits; zero means the width has not been specified yet.
  unsigned width = 0;
};

struct Expr {
  virtual ~Expr() = default;
  SourceLocation location;
};
using ExprPtr = std::unique_ptr<Expr>;

struct IntegerExpr final : Expr {
  explicit IntegerExpr(int64_t value) : value(value) {}
  int64_t value;
};
struct DurationExpr final : Expr {
  // Physical time in femtoseconds; target timing validation comes later.
  explicit DurationExpr(int64_t femtoseconds) : femtoseconds(femtoseconds) {}
  int64_t femtoseconds;
};
struct BooleanExpr final : Expr {
  explicit BooleanExpr(bool value) : value(value) {}
  bool value;
};
struct NameExpr final : Expr {
  explicit NameExpr(std::string name) : name(std::move(name)) {}
  std::string name;
};
struct BitSelectExpr final : Expr { ExprPtr value; ExprPtr bit; };
struct PinReadExpr final : Expr { std::string pin; };
struct CallExpr final : Expr {
  std::string callee;
  std::vector<ExprPtr> arguments;
};
struct UnaryExpr final : Expr {
  enum class Op { LogicalNot, BitwiseNot, Negate };
  Op op;
  ExprPtr operand;
};
struct BinaryExpr final : Expr {
  enum class Op {
    Add, Subtract, Multiply, Divide, Remainder,
    ShiftLeft, ShiftRight, BitwiseAnd, BitwiseOr, BitwiseXor,
    Equal, NotEqual, Less, LessEqual, Greater, GreaterEqual,
    LogicalAnd, LogicalOr
  };
  Op op;
  ExprPtr lhs;
  ExprPtr rhs;
};

struct Stmt {
  virtual ~Stmt() = default;
  SourceLocation location;
};
using StmtPtr = std::unique_ptr<Stmt>;

struct BlockStmt final : Stmt { std::vector<StmtPtr> statements; };
struct DriveStmt final : Stmt { std::string pin; ExprPtr value; };
struct OutputEnableStmt final : Stmt { std::string pin; ExprPtr value; };
// All changes in one update are requested at the same logical instant.
// The target checker must reject a target that cannot realize the timing.
struct PinUpdate {
  std::string pin;
  ExprPtr value;
  ExprPtr outputEnable;
};
struct UpdatePinsStmt final : Stmt { std::vector<PinUpdate> updates; };
struct DelayStmt final : Stmt { ExprPtr cycles; };
struct DelayForStmt final : Stmt { ExprPtr duration; };
struct WaitHighStmt final : Stmt { std::string pin; };
struct WaitEdgeStmt final : Stmt {
  enum class Edge { Rising, Falling, Either };
  std::string pin;
  Edge edge = Edge::Either;
  ExprPtr timeoutCycles;
};
struct WaitUntilStmt final : Stmt {
  ExprPtr condition;
  ExprPtr timeoutCycles;
};
struct SampleStmt final : Stmt { std::string pin; std::string result; };
struct SamplePinsStmt final : Stmt {
  std::vector<std::string> pins;
  std::string result;
};
struct ReadQueueStmt final : Stmt {
  std::string queue;
  std::string result;
};
struct WriteQueueStmt final : Stmt {
  std::string queue;
  ExprPtr value;
};
struct LetStmt final : Stmt {
  Type type;
  std::string name;
  ExprPtr initializer;
};
struct CallStmt final : Stmt {
  std::string callee;
  std::vector<ExprPtr> arguments;
};
struct IfStmt final : Stmt {
  ExprPtr condition;
  std::unique_ptr<BlockStmt> thenBlock;
  std::unique_ptr<BlockStmt> elseBlock;
};
struct RepeatStmt final : Stmt {
  ExprPtr count;
  std::unique_ptr<BlockStmt> body;
};
struct LoopStmt final : Stmt { std::unique_ptr<BlockStmt> body; };
struct WhileStmt final : Stmt {
  ExprPtr condition;
  std::unique_ptr<BlockStmt> body;
};
struct BreakStmt final : Stmt {};
struct ContinueStmt final : Stmt {};
// Each offset in a timed region is relative to the region's start. A target
// checker decides whether the requested time and concurrent work fit.
struct AtTimeStmt final : Stmt {
  ExprPtr offset;
  std::unique_ptr<BlockStmt> body;
};
struct TimedRegionStmt final : Stmt {
  std::unique_ptr<BlockStmt> body;
};
struct AssignStmt final : Stmt {
  std::string name;
  ExprPtr value;
  bool shiftLeft = false;
};
struct ReturnStmt final : Stmt { ExprPtr value; };

struct PinDeclaration {
  std::string name;
  // Physical placement is a target binding, so source may leave it unset.
  std::optional<unsigned> physicalIndex;
};
struct PinGroupDeclaration {
  std::string name;
  std::vector<std::string> pins;
};
struct ConstantDeclaration {
  Type type;
  std::string name;
  ExprPtr value;
};

struct Parameter { Type type; std::string name; };
struct Function {
  std::string name;
  std::vector<Parameter> parameters;
  Type resultType;
  std::unique_ptr<BlockStmt> body;
};
struct Transaction {
  std::string name;
  std::vector<Parameter> parameters;
  Type resultType;
  std::unique_ptr<BlockStmt> body;
};
struct Protocol {
  std::string name;
  std::vector<Parameter> parameters;
  std::vector<PinDeclaration> pins;
  std::vector<PinGroupDeclaration> pinGroups;
  std::vector<ConstantDeclaration> constants;
  std::vector<Function> functions;
  std::vector<Transaction> transactions;
};
struct Module { std::vector<Protocol> protocols; };

} // namespace ast
} // namespace protocol
