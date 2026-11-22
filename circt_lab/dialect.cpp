#include "dialect.h"
#include "mlir/IR/BuiltinAttributes.h"
#include "mlir/IR/BuiltinTypes.h"
#include "mlir/IR/Dialect.h"
#include "mlir/IR/DialectRegistry.h"
#include "mlir/IR/OpDefinition.h"
#include "mlir/Tools/Plugins/DialectPlugin.h"
#include "llvm/Support/JSON.h"
#include <set>
#include <string>

using namespace mlir;

namespace {
static bool positiveI64(Operation *op, StringRef name) {
  auto a = op->getAttrOfType<IntegerAttr>(name);
  return a && a.getType().isInteger(64) && a.getInt() > 0;
}
static bool nonnegativeI64(Operation *op, StringRef name) {
  auto a = op->getAttrOfType<IntegerAttr>(name);
  return a && a.getType().isInteger(64) && a.getInt() >= 0;
}
#define SPATIAL_OP_COMMON(CLASS, NAME) \
  using Op::Op; \
  MLIR_DEFINE_EXPLICIT_INTERNAL_INLINE_TYPE_ID(CLASS) \
  static StringRef getOperationName() { return NAME; } \
  static ArrayRef<StringRef> getAttributeNames() { return {}; }

class FabricOp : public Op<FabricOp, OpTrait::ZeroOperands, OpTrait::ZeroResults, OpTrait::ZeroRegions> {
public:
  SPATIAL_OP_COMMON(FabricOp, "spatial.fabric")
  LogicalResult verify() {
    Operation *op = getOperation();
    auto name = op->getAttrOfType<StringAttr>("name");
    if (!name || name.getValue().empty()) return emitOpError("requires a nonempty name");
    for (StringRef field : {"shift_width", "storage_words", "storage_word_bits", "control_states", "control_inputs", "route_cycles", "pad_max_output_hz_estimate", "pad_roundtrip_ns_estimate"})
      if (!positiveI64(op, field)) return emitOpError() << "requires positive i64 " << field;
    auto routes = op->getAttrOfType<ArrayAttr>("routes");
    if (!routes || routes.empty()) return emitOpError("requires nonempty routes");
    std::set<std::string> sinks;
    for (Attribute entry : routes) {
      auto route = dyn_cast<DictionaryAttr>(entry);
      if (!route) return emitOpError("route must be a dictionary");
      auto sink = route.getAs<StringAttr>("sink");
      auto sources = route.getAs<ArrayAttr>("sources");
      if (!sink || sink.getValue().empty() || !sinks.insert(sink.getValue().str()).second || !sources || sources.empty())
        return emitOpError("each route needs a unique nonempty sink and nonempty sources");
      std::set<std::string> seen;
      for (Attribute raw : sources) {
        auto src = dyn_cast<StringAttr>(raw);
        if (!src || src.getValue().empty() || !seen.insert(src.getValue().str()).second)
          return emitOpError("route sources must be unique nonempty strings");
      }
    }
    for (Attribute entry : routes) {
      auto route = cast<DictionaryAttr>(entry);
      for (Attribute raw : route.getAs<ArrayAttr>("sources")) {
        auto source = cast<StringAttr>(raw).getValue();
        if (source.starts_with("route.") && !sinks.count(source.drop_front(6).str()))
          return emitOpError() << "route references missing relay " << source;
      }
    }
    return success();
  }
};
class SpiOp : public Op<SpiOp, OpTrait::ZeroOperands, OpTrait::OneResult, OpTrait::ZeroRegions> {
public:
  SPATIAL_OP_COMMON(SpiOp, "spatial.spi")
  LogicalResult verify() {
    Operation *op = getOperation();
    auto target = op->getAttrOfType<StringAttr>("target");
    if (!target || target.getValue().empty()) return emitOpError("requires a nonempty target");
    for (StringRef field : {"width", "half_period", "clock_hz", "storage_words_required"})
      if (!positiveI64(op, field)) return emitOpError() << "requires positive i64 " << field;
    if (!nonnegativeI64(op, "tx_value")) return emitOpError("requires nonnegative i64 tx_value");
    auto width = op->getAttrOfType<IntegerAttr>("width").getInt();
    auto value = op->getAttrOfType<IntegerAttr>("tx_value").getInt();
    if (width > 1048576 || !op->getResult(0).getType().isSignlessInteger(static_cast<unsigned>(width)))
      return emitOpError("result type must be a signless integer matching width (1..1048576)");
    if (width < 63 && static_cast<uint64_t>(value) >= (uint64_t(1) << width))
      return emitOpError("tx_value does not fit width");
    auto connections = op->getAttrOfType<DictionaryAttr>("connections");
    if (!connections || connections.empty()) return emitOpError("requires nonempty connections");
    for (NamedAttribute entry : connections) {
      auto source = dyn_cast<StringAttr>(entry.getValue());
      if (!source || source.getValue().empty()) return emitOpError("connection sources must be nonempty strings");
    }
    return success();
  }
};
class ConfigWriteOp : public Op<ConfigWriteOp, OpTrait::ZeroOperands, OpTrait::ZeroResults, OpTrait::ZeroRegions> {
public:
  SPATIAL_OP_COMMON(ConfigWriteOp, "spatial.config_write")
  LogicalResult verify() {
    auto *op = getOperation();
    if (!nonnegativeI64(op,"address") || !nonnegativeI64(op,"value") || op->getAttrOfType<IntegerAttr>("value").getInt() > 65535)
      return emitOpError("requires nonnegative i64 address and 16-bit value");
    return success();
  }
};
class PayloadOp : public Op<PayloadOp, OpTrait::ZeroOperands, OpTrait::ZeroResults, OpTrait::ZeroRegions> {
public:
  SPATIAL_OP_COMMON(PayloadOp, "spatial.payload")
  LogicalResult verify() {
    auto *op = getOperation();
    if (!nonnegativeI64(op,"slot") || !nonnegativeI64(op,"value") || op->getAttrOfType<IntegerAttr>("value").getInt() > 65535)
      return emitOpError("requires nonnegative i64 slot and 16-bit value");
    return success();
  }
};
class FrontendItemOp : public Op<FrontendItemOp, OpTrait::ZeroOperands, OpTrait::ZeroResults, OpTrait::ZeroRegions> {
public:
  SPATIAL_OP_COMMON(FrontendItemOp, "spatial.frontend_item")
  LogicalResult verify() {
    auto kind = getOperation()->getAttrOfType<StringAttr>("kind");
    auto json = getOperation()->getAttrOfType<StringAttr>("item_json");
    if (!kind || kind.getValue().empty() || !json) return emitOpError("requires kind and item_json strings");
    auto parsed = llvm::json::parse(json.getValue());
    if (!parsed) { llvm::consumeError(parsed.takeError()); return emitOpError("item_json must be valid JSON"); }
    auto *obj = parsed->getAsObject();
    if (!obj || obj->getString("kind") != kind.getValue())
      return emitOpError("item_json kind must match the operation kind");
    return success();
  }
};
class FrontendInputOp : public Op<FrontendInputOp, OpTrait::ZeroOperands, OpTrait::ZeroResults, OpTrait::ZeroRegions> {
public:
  SPATIAL_OP_COMMON(FrontendInputOp, "spatial.frontend_input")
  LogicalResult verify() {
    auto consumer = getOperation()->getAttrOfType<StringAttr>("consumer");
    auto port = getOperation()->getAttrOfType<StringAttr>("port");
    auto json = getOperation()->getAttrOfType<StringAttr>("input_json");
    if (!consumer || consumer.getValue().empty() || !port || port.getValue().empty() || !json)
      return emitOpError("requires consumer, port and input_json strings");
    auto parsed = llvm::json::parse(json.getValue());
    if (!parsed) { llvm::consumeError(parsed.takeError()); return emitOpError("input_json must be valid JSON"); }
    auto *obj = parsed->getAsObject();
    auto *check = obj ? obj->getObject("check") : nullptr;
    auto status = check ? check->getString("status") : std::nullopt;
    if (!obj || obj->getString("consumer") != consumer.getValue() || obj->getString("port") != port.getValue() ||
        !obj->getArray("references") || !status || (*status != "valid" && *status != "invalid" && *status != "unknown"))
      return emitOpError("input_json must match consumer and port and contain references and check");
    return success();
  }
};
class FrontendStreamOp : public Op<FrontendStreamOp, OpTrait::ZeroOperands, OpTrait::ZeroResults, OpTrait::ZeroRegions> {
public:
  SPATIAL_OP_COMMON(FrontendStreamOp, "spatial.frontend_stream")
  LogicalResult verify() {
    auto name = getOperation()->getAttrOfType<StringAttr>("name");
    auto json = getOperation()->getAttrOfType<StringAttr>("stream_json");
    if (!name || name.getValue().empty() || !json) return emitOpError("requires name and stream_json strings");
    auto parsed = llvm::json::parse(json.getValue());
    if (!parsed) { llvm::consumeError(parsed.takeError()); return emitOpError("stream_json must be valid JSON"); }
    auto *obj = parsed->getAsObject();
    if (!obj || obj->getString("name") != name.getValue() || !obj->getObject("source") ||
        !obj->getObject("destination") || !obj->getArray("source_references") ||
        !obj->getArray("destination_references") || !obj->getArray("stages") ||
        !obj->getInteger("element_width_bits") || !obj->getBoolean("packets"))
      return emitOpError("stream_json must match name and contain endpoints, references, width, packet flag and stages");
    return success();
  }
};
// A typed, target-independent synchronous Mealy graph. Missing rows hold the
// current state and emit no action. Input bit 0 is the first declared input.
class ProtocolOp : public Op<ProtocolOp, OpTrait::ZeroOperands, OpTrait::ZeroResults, OpTrait::ZeroRegions> {
public:
  SPATIAL_OP_COMMON(ProtocolOp, "spatial.protocol")
  LogicalResult verify() {
    Operation *op = getOperation();
    auto name = op->getAttrOfType<StringAttr>("name");
    auto states = op->getAttrOfType<IntegerAttr>("states");
    auto initial = op->getAttrOfType<IntegerAttr>("initial_state");
    auto inputs = op->getAttrOfType<ArrayAttr>("inputs");
    auto rows = op->getAttrOfType<ArrayAttr>("rows");
    auto original = op->getAttrOfType<StringAttr>("protocol_json");
    if (!name || name.getValue().empty() || !positiveI64(op,"states") || !nonnegativeI64(op,"initial_state") ||
        !states || states.getInt()>256 || !initial || initial.getInt()>=states.getInt() ||
        !inputs || inputs.empty() || inputs.size()>8 || !rows || !original)
      return emitOpError("requires name, 1..256 states, valid initial state, 1..8 inputs, rows and protocol_json");
    auto json = llvm::json::parse(original.getValue());
    if (!json) { llvm::consumeError(json.takeError()); return emitOpError("protocol_json must be valid JSON"); }
    auto *obj = json->getAsObject();
    auto *jsonInputs = obj ? obj->getArray("inputs") : nullptr;
    auto *jsonRows = obj ? obj->getArray("transitions") : nullptr;
    if (!obj || obj->getString("name") != name.getValue() || obj->getInteger("states") != states.getInt() ||
        obj->getInteger("initial_state") != initial.getInt() || !jsonInputs || jsonInputs->size()!=inputs.size() ||
        !jsonRows || jsonRows->size()!=rows.size())
      return emitOpError("typed protocol does not match frontend protocol_json");
    for (size_t i=0; i<inputs.size(); ++i) {
      auto input = dyn_cast<StringAttr>(inputs[i]);
      auto *raw = (*jsonInputs)[i].getAsObject();
      if (!input || !raw) return emitOpError("invalid input");
      auto kind = raw->getString("kind");
      std::string expected;
      if (kind == "constant") {
        auto value = raw->getBoolean("value");
        if (!value) return emitOpError("constant input requires a boolean value");
        expected = *value ? "constant:1" : "constant:0";
      } else if (kind == "resource") {
        auto resource = raw->getString("name");
        if (!resource || resource->empty()) return emitOpError("resource input requires a name");
        expected = "resource:" + resource->str();
        if (auto output = raw->getString("output")) expected += "." + output->str();
      } else return emitOpError("unknown input kind");
      if (input.getValue() != expected) return emitOpError("typed input does not match frontend protocol_json");
    }
    std::set<std::pair<int64_t,int64_t>> seen;
    for (size_t i=0; i<rows.size(); ++i) {
      auto row = dyn_cast<DictionaryAttr>(rows[i]);
      auto *raw = (*jsonRows)[i].getAsObject();
      if (!row || !raw) return emitOpError("invalid transition row");
      auto from = row.getAs<IntegerAttr>("from"), mask = row.getAs<IntegerAttr>("input_mask");
      auto to = row.getAs<IntegerAttr>("to"), action = row.getAs<IntegerAttr>("action");
      if (!from || !mask || !to || !action || !from.getType().isInteger(64) || !mask.getType().isInteger(64) ||
          !to.getType().isInteger(64) || !action.getType().isInteger(64) || from.getInt()<0 || from.getInt()>=states.getInt() ||
          to.getInt()<0 || to.getInt()>=states.getInt() || mask.getInt()<0 || mask.getInt()>=(int64_t(1)<<inputs.size()) ||
          action.getInt()<0 || !seen.insert({from.getInt(),mask.getInt()}).second)
        return emitOpError("invalid or duplicate transition row");
      auto *rawInputs = raw->getArray("inputs");
      if (!rawInputs || rawInputs->size()!=inputs.size() || raw->getInteger("from")!=from.getInt() || raw->getInteger("to")!=to.getInt())
        return emitOpError("typed row does not match frontend protocol_json");
      int64_t expectedMask=0;
      for (size_t bit=0; bit<rawInputs->size(); ++bit) {
        auto v=(*rawInputs)[bit].getAsBoolean();
        if (!v) return emitOpError("frontend row input must be Boolean");
        if (*v) expectedMask |= int64_t(1)<<bit;
      }
      auto *rawAction=raw->get("action");
      if (!rawAction) return emitOpError("frontend row requires action or null");
      auto actionValue = rawAction->getAsInteger();
      if ((!rawAction->getAsNull() && !actionValue) || (actionValue && *actionValue<=0) ||
          action.getInt() != (actionValue ? *actionValue : 0) || mask.getInt()!=expectedMask)
        return emitOpError("typed action or input mask does not match frontend protocol_json");
    }
    return success();
  }
};
class FrontendProgramOp : public Op<FrontendProgramOp, OpTrait::ZeroOperands, OpTrait::ZeroResults,
                                    OpTrait::OneRegion, OpTrait::SingleBlock, OpTrait::NoTerminator> {
public:
  SPATIAL_OP_COMMON(FrontendProgramOp, "spatial.frontend_program")
  LogicalResult verify() {
    Operation *op = getOperation();
    auto source = op->getAttrOfType<StringAttr>("source");
    auto schema = op->getAttrOfType<StringAttr>("schema");
    auto clock = op->getAttrOfType<StringAttr>("clock_hz");
    uint64_t clockHz = 0;
    if (!source || source.getValue().empty() || !schema || schema.getValue() != "spatial.logical-program-ir.v1" ||
        !clock || clock.getValue().getAsInteger(10, clockHz) || clockHz == 0)
      return emitOpError("requires source, frontend v1 schema and positive integer clock_hz");
    for (StringRef field : {"protocols_json", "limitations_json", "connection_limitations_json"}) {
      auto json = op->getAttrOfType<StringAttr>(field);
      if (!json) return emitOpError() << "requires " << field;
      auto parsed = llvm::json::parse(json.getValue());
      if (!parsed) { llvm::consumeError(parsed.takeError()); return emitOpError() << field << " must be valid JSON"; }
      if (!parsed->getAsArray()) return emitOpError() << field << " must be a JSON array";
    }
    if (op->getRegion(0).empty()) return emitOpError("requires a body block");
    auto protocolsJson=llvm::json::parse(op->getAttrOfType<StringAttr>("protocols_json").getValue());
    if (!protocolsJson) { llvm::consumeError(protocolsJson.takeError()); return emitOpError("invalid protocols_json"); }
    auto *protocols=protocolsJson->getAsArray();
    size_t protocolIndex=0;
    for (Operation &item : op->getRegion(0).front()) {
      if (isa<ProtocolOp>(item)) {
        auto raw=item.getAttrOfType<StringAttr>("protocol_json");
        if (!raw || protocolIndex>=protocols->size()) return emitOpError("typed protocols must match frontend protocol list");
        auto parsed=llvm::json::parse(raw.getValue());
        if (!parsed) { llvm::consumeError(parsed.takeError()); return emitOpError("invalid typed protocol JSON"); }
        if (*parsed!=(*protocols)[protocolIndex++]) return emitOpError("typed protocols must match frontend protocol list");
      } else if (!isa<FrontendItemOp, FrontendInputOp, FrontendStreamOp>(item))
        return emitOpError("body contains an unsupported operation");
    }
    if (protocolIndex!=protocols->size()) return emitOpError("typed protocols must match frontend protocol list");
    return success();
  }
};
class SpatialDialect : public Dialect {
public:
  MLIR_DEFINE_EXPLICIT_INTERNAL_INLINE_TYPE_ID(SpatialDialect)
  explicit SpatialDialect(MLIRContext *ctx)
      : Dialect(getDialectNamespace(), ctx, TypeID::get<SpatialDialect>()) {
    addOperations<FabricOp, SpiOp, ConfigWriteOp, PayloadOp, ProtocolOp, FrontendProgramOp, FrontendItemOp, FrontendInputOp, FrontendStreamOp>();
  }
  static StringRef getDialectNamespace() { return "spatial"; }
};
} // namespace

void registerSpatialDialect(DialectRegistry &registry) { registry.insert<SpatialDialect>(); }
extern "C" LLVM_ATTRIBUTE_WEAK DialectPluginLibraryInfo mlirGetDialectPluginInfo() {
  return {MLIR_PLUGIN_API_VERSION, "SpatialDialect", "0.1", [](DialectRegistry *registry) {
    registerSpatialDialect(*registry);
  }};
}
