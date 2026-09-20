#pragma once

#include <cstdint>
#include <memory>
#include <string>
#include <utility>
#include <vector>

namespace protocol {
namespace ast {

struct SourceLocation { unsigned line = 0; unsigned column = 0; };

struct Type {
  enum class Kind { Void, Bool, Byte, Int, Pin };
  Kind kind = Kind::Void;
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
struct NameExpr final : Expr {
  explicit NameExpr(std::string name) : name(std::move(name)) {}
  std::string name;
};
struct BitSelectExpr final : Expr { ExprPtr value; unsigned bit = 0; };
struct UnaryExpr final : Expr {
  enum class Op { LogicalNot, Negate };
  Op op;
  ExprPtr operand;
};
struct BinaryExpr final : Expr {
  enum class Op { Add, ShiftLeft, Equal, LogicalAnd };
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
struct DelayStmt final : Stmt { ExprPtr cycles; };
struct WaitHighStmt final : Stmt { std::string pin; };
struct SampleStmt final : Stmt { std::string pin; std::string result; };
struct IfStmt final : Stmt {
  ExprPtr condition;
  std::unique_ptr<BlockStmt> thenBlock;
  std::unique_ptr<BlockStmt> elseBlock;
};
struct RepeatStmt final : Stmt {
  ExprPtr count;
  std::unique_ptr<BlockStmt> body;
};
struct AssignStmt final : Stmt {
  std::string name;
  ExprPtr value;
  bool shiftLeft = false;
};
struct ReturnStmt final : Stmt { ExprPtr value; };

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
  std::vector<Function> functions;
  std::vector<Transaction> transactions;
};
struct Module { std::vector<Protocol> protocols; };

} // namespace ast
} // namespace protocol
