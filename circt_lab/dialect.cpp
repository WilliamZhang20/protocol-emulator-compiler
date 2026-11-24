#include "dialect.h"
#include "mlir/IR/BuiltinAttributes.h"
#include "mlir/IR/BuiltinTypes.h"
#include "mlir/IR/Dialect.h"
#include "mlir/IR/DialectRegistry.h"
#include "mlir/IR/OpDefinition.h"
#include "mlir/Tools/Plugins/DialectPlugin.h"
#include "llvm/Support/JSON.h"
#include <set>
#include <map>
#include <cstdlib>
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

// Graphs use explicit MLIR integer types for every port; edges name ports,
// so a width mismatch is rejected before placement. They are experimental
// planning operations, not hardware or a bitstream.
class WorkloadGraphOp : public Op<WorkloadGraphOp, OpTrait::ZeroOperands, OpTrait::ZeroResults, OpTrait::ZeroRegions> {
public:
  SPATIAL_OP_COMMON(WorkloadGraphOp, "spatial.workload_graph")
  LogicalResult verify() {
    auto nodes=(*this)->getAttrOfType<ArrayAttr>("nodes"), edges=(*this)->getAttrOfType<ArrayAttr>("edges");
    if (!nodes || nodes.empty() || !edges) return emitOpError("requires nodes and edges arrays");
    if ((*this)->getAttr("analysis_only") && !(*this)->getAttrOfType<BoolAttr>("analysis_only"))
      return emitOpError("analysis_only must be Boolean");
    for (StringRef name : {"clock_hz", "half_period"}) {
      if (auto attr=(*this)->getAttr(name)) {
        auto value=dyn_cast<IntegerAttr>(attr);
        if (!value || !value.getType().isInteger(64) || value.getInt()<1 || value.getInt()>1000000000)
          return emitOpError() << name << " must be a positive bounded i64";
      }
    }
    if (auto rows=(*this)->getAttrOfType<ArrayAttr>("control_rows")) {
      if (rows.size()!=16) return emitOpError("control_rows must contain 16 entries");
      for (auto raw:rows) {
        auto row=dyn_cast<IntegerAttr>(raw);
        if (!row || !row.getType().isInteger(64) || row.getInt()<0 || row.getInt()>31)
          return emitOpError("control row must be a five-bit value");
      }
    }
    std::map<std::string, DictionaryAttr> byName;
    for (auto raw : nodes) {
      auto n=dyn_cast<DictionaryAttr>(raw);
      auto id=n ? n.getAs<StringAttr>("id") : StringAttr();
      auto resource=n ? n.getAs<StringAttr>("resource") : StringAttr();
      auto opcode=n ? n.getAs<StringAttr>("opcode") : StringAttr();
      auto ins=n ? n.getAs<ArrayAttr>("inputs") : ArrayAttr();
      auto outs=n ? n.getAs<ArrayAttr>("outputs") : ArrayAttr();
      auto duration=n ? n.getAs<IntegerAttr>("duration") : IntegerAttr();
      if (!n || !id || id.getValue().empty() || !resource || resource.getValue().empty() || !opcode || opcode.getValue().empty() ||
          !ins || !outs || !duration || !duration.getType().isInteger(64) || duration.getInt()<1 ||
          duration.getInt()>1000000 || !byName.emplace(id.getValue().str(),n).second)
        return emitOpError("invalid or duplicate node");
      for (auto list : {ins,outs}) for (auto t : list) {
        auto type=dyn_cast<TypeAttr>(t);
        if (!type || !type.getValue().isSignlessInteger() || cast<IntegerType>(type.getValue()).getWidth()>1024)
          return emitOpError("node ports must be signless integer types of at most 1024 bits");
      }
      if (auto address=n.getAs<IntegerAttr>("address"))
        if (!address.getType().isInteger(64) || address.getInt()<0) return emitOpError("invalid memory address");
      auto isWidth=[](Attribute value,unsigned bits) {
        return cast<TypeAttr>(value).getValue().isSignlessInteger(bits);
      };
      auto op=opcode.getValue();
      if (op=="protocol") {
        auto states=n.getAs<IntegerAttr>("states"), inputs=n.getAs<IntegerAttr>("control_inputs");
        if (!states || !inputs || !states.getType().isInteger(64) || !inputs.getType().isInteger(64) ||
            states.getInt()<1 || states.getInt()>1024 || inputs.getInt()<1 || inputs.getInt()>64)
          return emitOpError("protocol requires bounded states and control_inputs");
      }
      if (op=="timer") {
        auto period=n.getAs<IntegerAttr>("period");
        if (!period || !period.getType().isInteger(64) || period.getInt()<1 || period.getInt()>1000000000)
          return emitOpError("timer requires a positive bounded period");
      }
      if (op=="uart_timer") {
        auto period=n.getAs<IntegerAttr>("period"), first=n.getAs<IntegerAttr>("first");
        if (resource.getValue()!="timer" || !period || !first || !period.getType().isInteger(64) ||
            !first.getType().isInteger(64) || first.getInt()<1 || first.getInt()>period.getInt() ||
            period.getInt()>1000000 || ins.size()!=2 || outs.size()!=1 ||
            !isWidth(ins[0],1) || !isWidth(ins[1],1) || !isWidth(outs[0],1))
          return emitOpError("uart_timer requires two i1 controls, one i1 output and valid first/period cycles");
      }
      if (op=="uart_tx_8n1" && (resource.getValue()!="shifter" || ins.size()!=2 || outs.size()!=3 ||
          !isWidth(ins[0],8) || !isWidth(ins[1],1) || !isWidth(outs[0],1) || !isWidth(outs[1],1) || !isWidth(outs[2],1)))
        return emitOpError("uart_tx_8n1 requires i8 payload, i1 tick and three i1 outputs");
      if (op=="uart_rx_8n1" && (resource.getValue()!="shifter" || ins.size()!=3 || outs.size()!=2 ||
          !isWidth(ins[0],1) || !isWidth(ins[1],1) || !isWidth(ins[2],1) || !isWidth(outs[0],8) || !isWidth(outs[1],1)))
        return emitOpError("uart_rx_8n1 requires three i1 controls and i8 payload and i1 done outputs");
      if (op=="read" && (resource.getValue()!="memory" || !n.getAs<IntegerAttr>("address") ||
          outs.empty() || !isWidth(outs[0],8))) return emitOpError("read requires a memory address and i8 result");
      if (op=="write" && (resource.getValue()!="memory" || !n.getAs<IntegerAttr>("address") ||
          ins.empty() || !isWidth(ins[0],8))) return emitOpError("write requires a memory address and i8 input");
      if (op=="pack_big_endian_low_bits") {
        if (ins.size()!=2 || !isWidth(ins[0],8) || !isWidth(ins[1],8) || outs.size()!=2 || !isWidth(outs[1],1))
          return emitOpError("pack requires two i8 inputs, a word and an i1 event");
        auto width=cast<IntegerType>(cast<TypeAttr>(outs[0]).getValue()).getWidth();
        if (width<9 || width>16) return emitOpError("current two-byte pack supports 9..16 bits");
      }
      if (op=="unpack_big_endian_low_bits") {
        if (ins.size()!=1 || outs.size()!=2 || !isWidth(outs[0],8) || !isWidth(outs[1],8))
          return emitOpError("unpack requires a word and two i8 outputs");
        auto width=cast<IntegerType>(cast<TypeAttr>(ins[0]).getValue()).getWidth();
        if (width<9 || width>16) return emitOpError("current two-byte unpack supports 9..16 bits");
      }
      if (op=="shift_msb_first") {
        if (ins.size()!=5 || outs.size()!=4 || ins[0]!=outs[0]) return emitOpError("shift word ports must have the same type");
        for (size_t i=1;i<ins.size();++i) if (!isWidth(ins[i],1)) return emitOpError("shift control inputs must be i1");
        for (size_t i=1;i<outs.size();++i) if (!isWidth(outs[i],1)) return emitOpError("shift signal outputs must be i1");
      }
      if (n.get("exclusive") && !n.getAs<BoolAttr>("exclusive")) return emitOpError("exclusive must be Boolean");
      if (n.get("group") && !n.getAs<StringAttr>("group")) return emitOpError("group must be a string");
    }
    std::set<std::pair<std::string,int64_t>> connected;
    for (auto raw : edges) {
      auto e=dyn_cast<DictionaryAttr>(raw);
      auto src=e ? e.getAs<StringAttr>("from") : StringAttr();
      auto dst=e ? e.getAs<StringAttr>("to") : StringAttr();
      auto mode=e ? e.getAs<StringAttr>("mode") : StringAttr();
      auto sp=e ? e.getAs<IntegerAttr>("source_port") : IntegerAttr();
      auto dp=e ? e.getAs<IntegerAttr>("target_port") : IntegerAttr();
      if (!src || !dst || !mode || (mode.getValue()!="data" && mode.getValue()!="order" && mode.getValue()!="feedback") ||
          !sp || !dp || !sp.getType().isInteger(64) || !dp.getType().isInteger(64) || sp.getInt()<0 || dp.getInt()<0 ||
          !byName.count(src.getValue().str()) || !byName.count(dst.getValue().str())) return emitOpError("invalid edge");
      auto outs=byName.at(src.getValue().str()).getAs<ArrayAttr>("outputs");
      auto ins=byName.at(dst.getValue().str()).getAs<ArrayAttr>("inputs");
      if (sp.getInt()>=static_cast<int64_t>(outs.size()) || dp.getInt()>=static_cast<int64_t>(ins.size()) ||
          outs[sp.getInt()]!=ins[dp.getInt()] || !connected.emplace(dst.getValue().str(),dp.getInt()).second)
        return emitOpError("edge ports must exist, have identical types and have one driver");
    }
    for (auto &entry : byName) {
      auto ins=entry.second.getAs<ArrayAttr>("inputs");
      for (size_t i=0;i<ins.size();++i)
        if (!connected.count({entry.first,static_cast<int64_t>(i)})) return emitOpError("every input port must be connected");
    }
    return success();
  }
};

class GridRouteProblemOp : public Op<GridRouteProblemOp, OpTrait::ZeroOperands, OpTrait::ZeroResults, OpTrait::ZeroRegions> {
public:
  SPATIAL_OP_COMMON(GridRouteProblemOp, "spatial.grid_route_problem")
  LogicalResult verify() {
    auto rows=(*this)->getAttrOfType<IntegerAttr>("rows"), cols=(*this)->getAttrOfType<IntegerAttr>("columns");
    auto tracks=(*this)->getAttrOfType<ArrayAttr>("tracks"), nets=(*this)->getAttrOfType<ArrayAttr>("nets");
    auto layout=(*this)->getAttrOfType<ArrayAttr>("layout");
    auto sw=(*this)->getAttrOfType<ArrayAttr>("switch_selectors"), in=(*this)->getAttrOfType<ArrayAttr>("input_selectors");
    auto allReg=(*this)->getAttrOfType<BoolAttr>("all_registered");
    auto regLinks=(*this)->getAttrOfType<ArrayAttr>("registered_links");
    auto name=(*this)->getAttrOfType<StringAttr>("name");
    auto limit=(*this)->getAttrOfType<IntegerAttr>("search_limit");
    if (!name || name.getValue().empty() || !rows || !cols || !rows.getType().isInteger(64) || !cols.getType().isInteger(64) ||
        rows.getInt()<1 || rows.getInt()>16 || cols.getInt()<1 || cols.getInt()>16 || !tracks || tracks.empty() || tracks.size()>16 || !nets || !layout)
      return emitOpError("requires bounded grid, named problem, tracks, layout and nets");
    if (!limit || !limit.getType().isInteger(64) || limit.getInt()<1 || limit.getInt()>1000000) return emitOpError("invalid search limit");
    if (!sw || sw.size()!=5 || !in || in.size()!=5) return emitOpError("requires five switch and input selector values");
    for (auto raw:sw) { auto n=dyn_cast<IntegerAttr>(raw); if (!n || !n.getType().isInteger(64) || n.getInt()<0 || n.getInt()>7) return emitOpError("invalid switch selector"); }
    for (auto raw:in) { auto n=dyn_cast<IntegerAttr>(raw); if (!n || !n.getType().isInteger(64) || n.getInt()<0 || n.getInt()>255) return emitOpError("invalid input selector"); }
    if (cast<IntegerAttr>(sw[0]).getInt()!=0 || cast<IntegerAttr>(sw[1]).getInt()+3>7)
      return emitOpError("switch must encode zero and four local sources");
    if (!allReg || !regLinks || (allReg.getValue() && !regLinks.empty())) return emitOpError("invalid registered-link mode");
    std::set<std::pair<int64_t,int64_t>> seenLinks;
    for (auto raw:regLinks) {
      auto d=dyn_cast<DictionaryAttr>(raw); auto a=d ? d.getAs<IntegerAttr>("from") : IntegerAttr(), b=d ? d.getAs<IntegerAttr>("to") : IntegerAttr();
      if (!a || !b || !a.getType().isInteger(64) || !b.getType().isInteger(64) || a.getInt()<0 || b.getInt()<0 ||
          a.getInt()>=rows.getInt()*cols.getInt() || b.getInt()>=rows.getInt()*cols.getInt() ||
          std::abs(a.getInt()/cols.getInt()-b.getInt()/cols.getInt())+std::abs(a.getInt()%cols.getInt()-b.getInt()%cols.getInt())!=1 ||
          !seenLinks.emplace(a.getInt(),b.getInt()).second) return emitOpError("invalid or duplicate registered neighbor link");
    }
    for (auto t:tracks) { auto v=dyn_cast<StringAttr>(t); if (!v || (v.getValue()!="horizontal_then_vertical" && v.getValue()!="vertical_then_horizontal")) return emitOpError("unknown track turn class"); }
    std::map<std::string,int64_t> positions; std::set<std::string> names; std::set<std::pair<std::string,int64_t>> sinks, sources;
    for (auto raw:layout) {
      auto d=dyn_cast<DictionaryAttr>(raw); auto n=d ? d.getAs<StringAttr>("resource") : StringAttr(); auto c=d ? d.getAs<IntegerAttr>("cell") : IntegerAttr();
      if (!n || n.getValue().empty() || !c || !c.getType().isInteger(64) || c.getInt()<0 || c.getInt()>=rows.getInt()*cols.getInt() || !positions.emplace(n.getValue().str(),c.getInt()).second)
        return emitOpError("invalid or duplicate layout resource");
    }
    auto endpoint=[&](DictionaryAttr d, bool source)->bool {
      auto n=d ? d.getAs<StringAttr>("resource") : StringAttr(); auto p=d ? d.getAs<IntegerAttr>("port") : IntegerAttr();
      if (!n || !p || !p.getType().isInteger(64) || p.getInt()<0 || p.getInt()>(source ? 3 : 80) || !positions.count(n.getValue().str())) return false;
      return source ? sources.emplace(n.getValue().str(),p.getInt()).second : sinks.emplace(n.getValue().str(),p.getInt()).second;
    };
    if (nets.size()>256) return emitOpError("too many nets");
    for (auto raw:nets) {
      auto d=dyn_cast<DictionaryAttr>(raw); auto n=d ? d.getAs<StringAttr>("name") : StringAttr();
      auto source=d ? d.getAs<DictionaryAttr>("source") : DictionaryAttr(); auto dest=d ? d.getAs<ArrayAttr>("sinks") : ArrayAttr();
      if (!n || n.getValue().empty() || !names.insert(n.getValue().str()).second || !endpoint(source,true) || !dest || dest.empty() || dest.size()>64)
        return emitOpError("invalid net or duplicate source");
      for (auto rawSink:dest) if (!endpoint(dyn_cast<DictionaryAttr>(rawSink),false)) return emitOpError("invalid or multiply driven sink");
    }
    return success();
  }
};
class TargetGraphOp : public Op<TargetGraphOp, OpTrait::ZeroOperands, OpTrait::ZeroResults, OpTrait::ZeroRegions> {
public:
  SPATIAL_OP_COMMON(TargetGraphOp, "spatial.target_graph")
  LogicalResult verify() {
    auto tiles=(*this)->getAttrOfType<ArrayAttr>("tiles"), links=(*this)->getAttrOfType<ArrayAttr>("links");
    if (!tiles || tiles.empty() || !links) return emitOpError("requires tiles and links arrays");
    if (auto attr=(*this)->getAttr("pad_max_output_hz_estimate")) {
      auto value=dyn_cast<IntegerAttr>(attr);
      if (!value || !value.getType().isInteger(64) || value.getInt()<1 || value.getInt()>1000000000)
        return emitOpError("pad_max_output_hz_estimate must be a positive bounded i64");
    }
    std::set<std::string> ids;
    for (auto raw:tiles) {
      auto t=dyn_cast<DictionaryAttr>(raw); auto id=t ? t.getAs<StringAttr>("id") : StringAttr();
      auto resource=t ? t.getAs<StringAttr>("resource") : StringAttr();
      auto operations=t ? t.getAs<ArrayAttr>("operations") : ArrayAttr();
      auto width=t ? t.getAs<IntegerAttr>("width") : IntegerAttr();
      auto slots=t ? t.getAs<IntegerAttr>("slots") : IntegerAttr();
      auto latency=t ? t.getAs<IntegerAttr>("latency") : IntegerAttr();
      auto interval=t ? t.getAs<IntegerAttr>("interval") : IntegerAttr();
      if (!id || id.getValue().empty() || !resource || resource.getValue().empty() || !operations || operations.empty() || !width || !slots || !latency || !interval ||
          !width.getType().isInteger(64) || width.getInt()<1 || width.getInt()>1024 ||
          !slots.getType().isInteger(64) || slots.getInt()!=1 ||
          !latency.getType().isInteger(64) || latency.getInt()<1 || latency.getInt()>1000000 ||
          !interval.getType().isInteger(64) || interval.getInt()<1 || interval.getInt()>1000000 ||
          !ids.insert(id.getValue().str()).second)
        return emitOpError("invalid or duplicate tile (only one slot per tile is modeled)");
      std::set<std::string> supported;
      for (auto rawOp:operations) {
        auto op=dyn_cast<StringAttr>(rawOp);
        if (!op || op.getValue().empty() || !supported.insert(op.getValue().str()).second)
          return emitOpError("tile operations must be unique nonempty strings");
      }
      if (supported.count("protocol")) {
        auto states=t.getAs<IntegerAttr>("states"), inputs=t.getAs<IntegerAttr>("control_inputs");
        if (!states || !inputs || !states.getType().isInteger(64) || !inputs.getType().isInteger(64) ||
            states.getInt()<1 || states.getInt()>1024 || inputs.getInt()<1 || inputs.getInt()>64)
          return emitOpError("protocol tile requires bounded states and control_inputs");
      }
      if (supported.count("timer") || supported.count("uart_timer")) {
        auto min=t.getAs<IntegerAttr>("period_min"), max=t.getAs<IntegerAttr>("period_max");
        if (!min || !max || !min.getType().isInteger(64) || !max.getType().isInteger(64) ||
            min.getInt()<1 || max.getInt()<min.getInt() || max.getInt()>1000000000)
          return emitOpError("timer tile requires valid period_min and period_max");
      }
      if (supported.count("uart_timer")) {
        auto min=t.getAs<IntegerAttr>("first_min"), max=t.getAs<IntegerAttr>("first_max");
        if (!min || !max || !min.getType().isInteger(64) || !max.getType().isInteger(64) ||
            min.getInt()<1 || max.getInt()<min.getInt() || max.getInt()>1000000000)
          return emitOpError("uart_timer tile requires valid first_min and first_max");
      }
      if (auto depth=t.getAs<IntegerAttr>("depth"))
        if (!depth.getType().isInteger(64) || depth.getInt()<1 || depth.getInt()>1000000) return emitOpError("invalid tile depth");
    }
    std::set<std::pair<std::string,std::string>> linksSeen;
    for (auto raw:links) {
      auto l=dyn_cast<DictionaryAttr>(raw); auto from=l ? l.getAs<StringAttr>("from") : StringAttr();
      auto to=l ? l.getAs<StringAttr>("to") : StringAttr();
      auto width=l ? l.getAs<IntegerAttr>("width") : IntegerAttr();
      auto latency=l ? l.getAs<IntegerAttr>("latency") : IntegerAttr();
      if (!from || !to || !ids.count(from.getValue().str()) || !ids.count(to.getValue().str()) ||
          !width || !width.getType().isInteger(64) || width.getInt()<1 || width.getInt()>1024 ||
          !latency || !latency.getType().isInteger(64) || latency.getInt()<0 || latency.getInt()>1000000 ||
          !linksSeen.emplace(from.getValue().str(),to.getValue().str()).second)
        return emitOpError("invalid or duplicate directed link");
    }
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
    addOperations<FabricOp, SpiOp, ConfigWriteOp, PayloadOp, WorkloadGraphOp, GridRouteProblemOp, TargetGraphOp, ProtocolOp, FrontendProgramOp, FrontendItemOp, FrontendInputOp, FrontendStreamOp>();
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
