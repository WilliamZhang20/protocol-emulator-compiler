// Experimental MLIR/CIRCT mapping of one protocol onto the RTL tile slice.
// Registered spatial operations check their own target-independent invariants.
#include "dialect.h"
#include "mlir/IR/BuiltinOps.h"
#include "mlir/IR/Builders.h"
#include "mlir/IR/Verifier.h"
#include "mlir/Parser/Parser.h"
#include "llvm/Support/raw_ostream.h"
#include "llvm/Support/JSON.h"
#include "llvm/Support/FormatVariadic.h"
#include "llvm/ADT/StringRef.h"
#include "circt/Dialect/HW/HWDialect.h"
#include "circt/Dialect/Comb/CombDialect.h"
#include "circt/Dialect/FSM/FSMDialect.h"
#include "circt/Dialect/FSM/FSMOps.h"
#include <cstdint>
#include <array>
#include <algorithm>
#include <fstream>
#include <iomanip>
#include <functional>
#include <map>
#include <deque>
#include <queue>
#include <set>
#include <string>
#include <vector>

using namespace mlir;

static int fail(const std::string &message) {
  llvm::errs() << "mapping error: " << message << "\n";
  return 1;
}

static Operation *findOne(ModuleOp module, llvm::StringRef name) {
  Operation *result = nullptr;
  for (Operation &op : module.getBody()->getOperations()) {
    if (op.getName().getStringRef() == name) {
      if (result) return nullptr;
      result = &op;
    } else {
      return nullptr;
    }
  }
  return result;
}

static bool integerAttr(Operation *op, llvm::StringRef name, int64_t &value) {
  auto attr = op->getAttrOfType<IntegerAttr>(name);
  if (!attr || !attr.getType().isInteger(64)) return false;
  value = attr.getInt();
  return true;
}

// Reconstruct the Rust frontend's serialized logical program and derived
// connection graph from verified MLIR operations. This preserves the input;
// it does not lower or claim to understand the source program semantics.
static int exportFrontend(ModuleOp module) {
  Operation *root = findOne(module, "spatial.frontend_program");
  if (!root) return fail("expected exactly one spatial.frontend_program");
  auto parseStringJson = [](StringAttr value) -> llvm::json::Value {
    auto parsed = llvm::json::parse(value.getValue());
    // The registered op verifier has already checked all these fields.
    if (!parsed) llvm::report_fatal_error("verified frontend JSON became invalid");
    return std::move(*parsed);
  };
  llvm::json::Array items, inputs, streams;
  for (Operation &op : root->getRegion(0).front()) {
    auto kind = op.getName().getStringRef();
    if (kind == "spatial.frontend_item") items.push_back(parseStringJson(op.getAttrOfType<StringAttr>("item_json")));
    else if (kind == "spatial.protocol") continue; // Preserved in protocols_json and cross-checked by verifier.
    else if (kind == "spatial.frontend_input") inputs.push_back(parseStringJson(op.getAttrOfType<StringAttr>("input_json")));
    else if (kind == "spatial.frontend_stream") streams.push_back(parseStringJson(op.getAttrOfType<StringAttr>("stream_json")));
    else return fail("unsupported frontend child operation");
  }
  llvm::json::Object graph{{"inputs", std::move(inputs)}, {"streams", std::move(streams)},
      {"limitations", parseStringJson(root->getAttrOfType<StringAttr>("connection_limitations_json"))}};
  llvm::json::Object result{{"schema", root->getAttrOfType<StringAttr>("schema").getValue()},
      {"clock_hz", root->getAttrOfType<StringAttr>("clock_hz").getValue()}, {"items", std::move(items)},
      {"protocols", parseStringJson(root->getAttrOfType<StringAttr>("protocols_json"))},
      {"limitations", parseStringJson(root->getAttrOfType<StringAttr>("limitations_json"))},
      {"connections", std::move(graph)}};
  llvm::outs() << llvm::formatv("{0:2}\n", llvm::json::Value(std::move(result)));
  return 0;
}

// Lower one verified frontend protocol graph to the actual experimental
// proto_control's 16-row table. This is a component mapping, not a full
// program image: inputs and action outputs still need placement and routes.
static int mapControl(ModuleOp module, llvm::StringRef requested, const char *output,
                      int outputMask = -1, llvm::json::Object pinBindings = {},
                      std::vector<uint16_t> *resultTable = nullptr) {
  Operation *root = findOne(module, "spatial.frontend_program");
  if (!root) return fail("expected exactly one spatial.frontend_program");
  Operation *protocol = nullptr;
  for (Operation &op : root->getRegion(0).front()) {
    if (op.getName().getStringRef() != "spatial.protocol") continue;
    if (op.getAttrOfType<StringAttr>("name").getValue() == requested) {
      if (protocol) return fail("duplicate protocol name");
      protocol = &op;
    }
  }
  if (!protocol) return fail("requested protocol not found");
  int64_t states, initial;
  if (!integerAttr(protocol,"states",states) || !integerAttr(protocol,"initial_state",initial))
    return fail("invalid protocol state information");
  auto inputs=protocol->getAttrOfType<ArrayAttr>("inputs");
  auto rows=protocol->getAttrOfType<ArrayAttr>("rows");
  if (inputs.size()>2) return fail("prototype control tile supports at most two inputs");
  struct Step { int next; int64_t action; };
  const int combinations = 1 << inputs.size();
  std::vector<std::vector<Step>> machine(states, std::vector<Step>(combinations));
  for (int state=0; state<states; ++state)
    for (int mask=0; mask<combinations; ++mask) machine[state][mask] = {state, 0};
  for (Attribute raw : rows) {
    auto row=cast<DictionaryAttr>(raw);
    auto state=row.getAs<IntegerAttr>("from").getInt();
    auto mask=row.getAs<IntegerAttr>("input_mask").getInt();
    machine[state][mask] = {static_cast<int>(row.getAs<IntegerAttr>("to").getInt()),
                            row.getAs<IntegerAttr>("action").getInt()};
  }
  std::vector<bool> reachable(states, false);
  std::deque<int> queue;
  reachable[initial]=true;
  queue.push_back(static_cast<int>(initial));
  while (!queue.empty()) {
    int state=queue.front(); queue.pop_front();
    for (const Step &step : machine[state]) {
      if (!reachable[step.next]) { reachable[step.next]=true; queue.push_back(step.next); }
    }
  }
  // Refine equivalence by each input's emitted action and destination class.
  // This is lossless for synchronous Mealy graphs; unreachable states do not
  // consume target rows. All combinations are considered conservatively.
  std::vector<int> classes(states, -1);
  for (int state=0; state<states; ++state) if (reachable[state]) classes[state]=0;
  while (true) {
    std::map<std::vector<int64_t>, int> signatures;
    std::vector<int> nextClasses(states, -1);
    for (int state=0; state<states; ++state) if (reachable[state]) {
      std::vector<int64_t> signature;
      for (const Step &step : machine[state]) {
        if (step.action>3) return fail("prototype control tile has only three action outputs");
        signature.push_back(step.action); signature.push_back(classes[step.next]);
      }
      auto entry=signatures.emplace(signature, static_cast<int>(signatures.size()));
      nextClasses[state]=entry.first->second;
    }
    if (nextClasses==classes) break;
    classes=std::move(nextClasses);
  }
  int classCount=0, reachableCount=0;
  for (int state=0; state<states; ++state) if (reachable[state]) {
    ++reachableCount; classCount=std::max(classCount, classes[state]+1);
  }
  if (classCount>4) return fail("minimized protocol exceeds prototype control tile's four states");
  std::vector<int> classToTarget(classCount, -1);
  classToTarget[classes[initial]]=0;
  int nextTarget=1;
  for (int c=0; c<classCount; ++c) if (classToTarget[c]<0) classToTarget[c]=nextTarget++;
  std::vector<int> sourceToTarget(states, -1);
  std::vector<int> representative(classCount, -1);
  for (int state=0; state<states; ++state) if (reachable[state]) {
    int target=classToTarget[classes[state]];
    sourceToTarget[state]=target;
    if (representative[target]<0) representative[target]=state;
  }
  std::vector<uint16_t> table(16);
  for (int state=0; state<4; ++state)
    for (int a=0; a<2; ++a)
      for (int b=0; b<2; ++b) table[(state<<2)|(a<<1)|b]=static_cast<uint16_t>(state);
  for (int target=0; target<classCount; ++target) {
    int sourceState=representative[target];
    for (int a=0; a<2; ++a) for (int b=0; b<2; ++b) {
      int sourceMask=a | (inputs.size()==2 ? b<<1 : 0);
      Step step=machine[sourceState][sourceMask];
      int outputBits=step.action==0 ? 0 : (1 << (step.action-1));
      table[(target<<2)|(a<<1)|b]=static_cast<uint16_t>(sourceToTarget[step.next] | (outputBits<<2));
    }
  }
  if (resultTable) { *resultTable = table; return 0; }
  std::ofstream image(output);
  if (!image) return fail("cannot open control-table output");
  for (uint16_t row : table) image << std::hex << std::setfill('0') << std::setw(4) << row << '\n';
  if (outputMask >= 0) image << std::hex << std::setfill('0') << std::setw(4) << outputMask << '\n';
  if (!image) return fail("cannot write control-table output");
  llvm::json::Array bindings;
  for (Attribute input : inputs) bindings.push_back(cast<StringAttr>(input).getValue());
  llvm::json::Array stateMap;
  for (int mapped : sourceToTarget)
    if (mapped<0) stateMap.push_back(nullptr); else stateMap.push_back(mapped);
  llvm::json::Object report{{"component", "prototype-control-v1"}, {"protocol", requested},
      {"source_inputs", std::move(bindings)}, {"control_table_written", true},
      {"source_states", states}, {"reachable_states", reachableCount},
      {"mapped_states", classCount}, {"state_map", std::move(stateMap)},
      {"input_binding_verified", false}, {"full_program_compiled", false},
      {"limitations", "Only a control table; no source input binding, placement, surrounding program, routing, timing or full-fabric image."}};
  if (outputMask >= 0) {
    report["component"] = "control-fabric-v1";
    report["input_binding_verified"] = true;
    report["full_program_compiled"] = true;
    report["pin_bindings"] = std::move(pinBindings);
    report["limitations"] = "Restricted control-only prototype; no host transport or physical timing proof.";
  }
  llvm::outs() << llvm::formatv("{0:2}\n", llvm::json::Value(std::move(report)));
  return 0;
}

// Reject every construct whose semantics the small, fixed control fabric does
// not implement. Validation happens before opening the output image.
static int compileControl(ModuleOp module, const char *output) {
  Operation *root = findOne(module, "spatial.frontend_program");
  if (!root) return fail("expected exactly one spatial.frontend_program");
  Operation *protocol = nullptr;
  std::vector<llvm::json::Value> items;
  for (Operation &op : root->getRegion(0).front()) {
    auto kind = op.getName().getStringRef();
    if (kind == "spatial.protocol") {
      if (protocol) return fail("control fabric requires exactly one protocol");
      protocol = &op;
    } else if (kind == "spatial.frontend_item") {
      auto parsed = llvm::json::parse(op.getAttrOfType<StringAttr>("item_json").getValue());
      if (!parsed) return fail("invalid item JSON");
      items.push_back(std::move(*parsed));
    } else if (kind == "spatial.frontend_input") {
      auto parsed = llvm::json::parse(op.getAttrOfType<StringAttr>("input_json").getValue());
      if (!parsed) return fail("invalid input JSON");
      auto *obj = parsed->getAsObject();
      auto *check = obj ? obj->getObject("check") : nullptr;
      if (!check || check->getString("status") != "valid" || !obj->get("context") || !obj->get("context")->getAsNull())
        return fail("control fabric requires valid base-context inputs");
    } else return fail("control fabric does not support streams or unknown frontend operations");
  }
  if (!protocol) return fail("control fabric requires exactly one protocol");
  auto protocolName = protocol->getAttrOfType<StringAttr>("name").getValue();
  std::set<std::string> names, inPins, usedRegs, usedActions;
  std::map<std::string, int> regs;
  std::vector<std::pair<std::string, std::string>> outs;
  int clocks = 0, declarations = 0;
  auto isNull = [](llvm::json::Object *o, llvm::StringRef key) {
    auto *v = o->get(key); return v && v->getAsNull().has_value();
  };
  for (auto &item : items) {
    auto *o = item.getAsObject();
    if (!o) return fail("invalid frontend item");
    auto kind = o->getString("kind");
    if (!kind) return fail("item missing kind");
    if (*kind == "clock") { ++clocks; continue; }
    auto name = o->getString("name");
    if (!name || !names.insert(name->str()).second) return fail("missing or duplicate resource name");
    if (*kind == "protocol") {
      if (*name != protocolName) return fail("protocol declaration mismatch");
      ++declarations;
    } else if (*kind == "pin") {
      auto mode = o->getString("mode");
      if (!mode || !isNull(o, "pads")) return fail("unsupported pin binding");
      if (*mode == "in" && isNull(o, "source")) inPins.insert(name->str());
      else if (*mode == "out") {
        auto *source = o->getObject("source");
        if (!source || source->getString("kind") != "name" || !source->getString("name"))
          return fail("output must directly reference a register");
        outs.emplace_back(name->str(), source->getString("name")->str());
      } else return fail("unsupported pin mode or input source");
    } else if (*kind == "register") {
      auto *value = o->getObject("value");
      auto *object = value ? value->getObject("object") : nullptr;
      auto field = value ? value->getString("field") : std::nullopt;
      if (!isNull(o, "initial") || !isNull(o, "clock_enable") || !value || value->getString("kind") != "field" ||
          !object || object->getString("kind") != "name" || object->getString("name") != protocolName ||
          !field || (*field != "action1" && *field != "action2" && *field != "action3") ||
          !usedActions.insert(field->str()).second)
        return fail("register must uniquely capture a protocol action");
      regs[name->str()] = (*field)[6] - '1';
    } else return fail("control fabric does not support source item: " + kind->str());
  }
  if (clocks != 1 || declarations != 1 || outs.empty()) return fail("control fabric requires one clock, one protocol and output pins");
  llvm::json::Object bindings;
  auto inputs = protocol->getAttrOfType<ArrayAttr>("inputs");
  if (inputs.empty() || inputs.size() > 2 || inputs.size() != inPins.size()) return fail("control fabric requires one or two bound input pins");
  for (auto indexed : llvm::enumerate(inputs)) {
    auto name = cast<StringAttr>(indexed.value()).getValue();
    if (!name.consume_front("resource:") || inPins.erase(name.str()) != 1) return fail("protocol inputs must be distinct input pins");
    bindings[name] = "input" + std::to_string(indexed.index());
  }
  int mask = 0;
  for (const auto &pin : outs) {
    auto reg = regs.find(pin.second);
    if (reg == regs.end() || !usedRegs.insert(pin.second).second) return fail("each output must uniquely reference an action register");
    mask |= 1 << reg->second;
    bindings[pin.first] = "output" + std::to_string(reg->second);
  }
  if (usedRegs.size() != regs.size()) return fail("unused action register");
  return mapControl(module, protocolName, output, mask, std::move(bindings));
}

// Derive a *component experiment*, not a complete SPI implementation: the
// host supplies a payload and trigger, and the existing hand-authored control
// machine supplies transaction control. Reject unsupported frame/timer shapes.
static int deriveSpiComponent(ModuleOp module, llvm::StringRef timerName,
                              llvm::StringRef shiftName, llvm::StringRef txText,
                              llvm::raw_ostream &out = llvm::outs(), bool report = true) {
  Operation *root = findOne(module, "spatial.frontend_program");
  if (!root) return fail("expected exactly one spatial.frontend_program");
  std::vector<llvm::json::Value> items;
  for (Operation &op : root->getRegion(0).front()) {
    if (op.getName().getStringRef() != "spatial.frontend_item") continue;
    auto item = llvm::json::parse(op.getAttrOfType<StringAttr>("item_json").getValue());
    if (!item) return fail("invalid frontend item");
    items.push_back(std::move(*item));
  }
  llvm::json::Object *timer = nullptr, *shift = nullptr;
  std::map<std::string, llvm::json::Object *> constants;
  std::set<std::string> pins;
  for (auto &v : items) {
    auto *o = v.getAsObject();
    if (!o) return fail("invalid item");
    auto kind = o->getString("kind"), name = o->getString("name");
    if (!kind || !name) continue;
    if (*kind == "constant") {
      if (!constants.emplace(name->str(), o).second) return fail("duplicate constant");
    } else if (*kind == "pin" && o->getString("mode") == "in") pins.insert(name->str());
    else if (*kind == "resource" && *name == timerName && o->getString("resource_kind") == "timer") {
      if (timer) return fail("duplicate timer"); timer = o;
    } else if (*kind == "resource" && *name == shiftName && o->getString("resource_kind") == "shifter") {
      if (shift) return fail("duplicate shifter"); shift = o;
    }
  }
  if (!timer || !shift || timerName == shiftName) return fail("timer or shifter not found");
  auto fields = [](llvm::json::Object *o, std::map<std::string, llvm::json::Object *> &out) {
    auto *a = o->getArray("fields"); if (!a) return false;
    for (auto &entry : *a) {
      auto *field = entry.getAsObject();
      if (!field || !field->getString("name") || !field->getObject("value") ||
          !out.emplace(field->getString("name")->str(), field->getObject("value")).second) return false;
    }
    return true;
  };
  std::map<std::string, llvm::json::Object *> tf, sf;
  if (!fields(timer, tf) || !fields(shift, sf) || tf.size()!=3 || sf.size()!=4 ||
      !tf.count("every") || !tf.count("idle") || !tf.count("run") ||
      !sf.count("frame") || !sf.count("start") || !sf.count("shift") || !sf.count("sample"))
    return fail("unsupported timer or shifter fields");
  auto fieldRef = [](llvm::json::Object *o, llvm::StringRef resource, llvm::StringRef field) {
    auto *object = o ? o->getObject("object") : nullptr;
    return o && o->getString("kind") == "field" && o->getString("field") == field && object &&
        object->getString("kind") == "name" && object->getString("name") == resource;
  };
  auto *run = tf["run"], *sample = sf["sample"];
  if (tf["idle"]->getString("kind") != "integer" || tf["idle"]->getString("value") != "0" ||
      run->getString("kind") != "binary" || run->getString("op") != "until" ||
      !fieldRef(run->getObject("left"), shiftName, "busy") || !fieldRef(run->getObject("right"), shiftName, "done") ||
      !fieldRef(sf["shift"], timerName, "fall") || sample->getString("kind") != "binary" ||
      sample->getString("op") != "at" || !fieldRef(sample->getObject("right"), timerName, "rise"))
    return fail("timer/shifter relationship is not the supported SPI component");
  auto *input = sample->getObject("left");
  if (!input || input->getString("kind") != "name" || !input->getString("name") ||
      !pins.count(input->getString("name")->str())) return fail("sample must reference an input pin");
  auto *period = tf["every"];
  if (period->getString("kind") == "name") {
    auto name = period->getString("name");
    auto found = name ? constants.find(name->str()) : constants.end();
    if (found == constants.end() || !(period = found->second->getObject("value"))) return fail("unresolved timer constant");
  }
  int64_t half = 0, width = 0, tx = 0, clock = 0;
  auto periodValue = period->getString("value");
  if (period->getString("kind") != "quantity" || period->getString("unit") != "cycles" ||
      !periodValue || periodValue->getAsInteger(10, half) || half < 8 || half > 255)
    return fail("timer period must be 8..255 whole cycles");
  auto *frame = sf["frame"];
  auto *values = frame->getArray("values");
  auto *call = values && values->size()==1 ? (*values)[0].getAsObject() : nullptr;
  auto *callee = call ? call->getObject("callee") : nullptr;
  auto *args = call ? call->getArray("args") : nullptr;
  auto *arg0 = args && args->size()==2 ? (*args)[0].getAsObject() : nullptr;
  auto *arg1 = args && args->size()==2 ? (*args)[1].getAsObject() : nullptr;
  auto *bits = arg0 ? arg0->getObject("value") : nullptr;
  auto *order = arg1 ? arg1->getObject("value") : nullptr;
  auto bitValue = bits ? bits->getString("value") : std::nullopt;
  if (frame->getString("kind") != "list" || !call || call->getString("kind") != "call" ||
      !callee || callee->getString("kind") != "name" || callee->getString("name") != "data" ||
      !arg0 || !arg1 || !arg0->get("name") || !arg0->get("name")->getAsNull() ||
      !arg1->get("name") || !arg1->get("name")->getAsNull() || !bits || bits->getString("kind") != "integer" ||
      !bitValue || bitValue->getAsInteger(10, width) || width < 1 || width > 16 || !order ||
      order->getString("kind") != "name" || order->getString("name") != "msb_first")
    return fail("frame must be one 1..16-bit MSB-first data field");
  if (txText.getAsInteger(0, tx) || tx < 0 || tx >= (int64_t(1) << width) ||
      root->getAttrOfType<StringAttr>("clock_hz").getValue().getAsInteger(10, clock) || clock <= 0)
    return fail("invalid payload or source clock");
  out << "module {\n  %received = \"spatial.spi\"() {\n"
      << "    target = \"spi-fabric-v1\", width = " << width << " : i64, half_period = " << half << " : i64,\n"
      << "    clock_hz = " << clock << " : i64, tx_value = " << tx << " : i64, storage_words_required = 2 : i64,\n"
      << "    connections = {start = \"control.start\", run = \"shift.busy\", rise = \"timer.rise\", fall = \"timer.fall\", input = \"pin.miso\", mosi = \"shift.out\", sck = \"timer.level\", cs = \"control.not_selected\"}\n"
      << "  } : () -> i" << width << "\n}\n";
  if (report) llvm::errs() << "Component experiment only: source transaction control, input binding, streams and full-program semantics are not mapped.\n";
  return 0;
}

// A strict source-driven control/datapath component for the experimental
// memory-backed SPI fabric. SRAM layout, host trigger staging and transport
// are still supplied externally; this deliberately does not claim complete
// compilation of arbitrary .spt transaction functions or byte streams.
static int compileMemorySpi(ModuleOp module, llvm::StringRef txText, const char *output, bool graphOnly=false, llvm::raw_ostream &graphOut=llvm::outs()) {
  Operation *root = findOne(module, "spatial.frontend_program");
  if (!root) return fail("expected frontend program");
  std::vector<llvm::json::Value> items;
  Operation *protocol = nullptr;
  for (Operation &op : root->getRegion(0).front()) {
    auto kind = op.getName().getStringRef();
    if (kind == "spatial.frontend_item") {
      auto item = llvm::json::parse(op.getAttrOfType<StringAttr>("item_json").getValue());
      if (!item) return fail("invalid frontend item");
      items.push_back(std::move(*item));
    } else if (kind == "spatial.protocol") {
      if (protocol) return fail("exactly one protocol is supported");
      protocol = &op;
    } else if (kind == "spatial.frontend_input") {
      auto input = llvm::json::parse(op.getAttrOfType<StringAttr>("input_json").getValue());
      auto *obj = input ? input->getAsObject() : nullptr;
      auto *check = obj ? obj->getObject("check") : nullptr;
      if (!check || check->getString("status") != "valid" || !obj->get("context") || !obj->get("context")->getAsNull())
        return fail("all source connections must be valid in the base context");
    } else return fail("source streams and unknown operations are not supported");
  }
  if (!protocol || protocol->getAttrOfType<StringAttr>("name").getValue() != "transfer_control")
    return fail("expected transfer_control protocol");
  auto inputs = protocol->getAttrOfType<ArrayAttr>("inputs");
  if (inputs.size()!=2 || cast<StringAttr>(inputs[0]).getValue()!="resource:trigger" ||
      cast<StringAttr>(inputs[1]).getValue()!="resource:bits.done")
    return fail("control inputs must be trigger and bits.done in that order");
  std::map<std::string, llvm::json::Object *> named;
  int clocks=0;
  for (auto &v : items) {
    auto *obj=v.getAsObject();
    if (!obj || !obj->getString("kind")) return fail("invalid source item");
    if (obj->getString("kind")=="clock") { ++clocks; continue; }
    auto name=obj->getString("name");
    if (!name || !named.emplace(name->str(),obj).second) return fail("unnamed or duplicate source item");
  }
  const std::set<std::string> expected={"trigger","miso","cs_n","cs","sck","mosi","transfer_control","sck_clock","bits"};
  std::set<std::string> actual;
  for (const auto &entry : named) actual.insert(entry.first);
  if (clocks!=1 || actual!=expected) return fail("unsupported source declarations in memory SPI component");
  auto isNull=[](llvm::json::Object *o, llvm::StringRef key) {
    auto *v=o->get(key); return v && v->getAsNull().has_value();
  };
  auto field=[](llvm::json::Object *o, llvm::StringRef resource, llvm::StringRef port) {
    auto *base=o ? o->getObject("object") : nullptr;
    return o && o->getString("kind")=="field" && o->getString("field")==port &&
        base && base->getString("kind")=="name" && base->getString("name")==resource;
  };
  for (const char *name : {"trigger","miso"}) {
    auto *pin=named[name];
    if (pin->getString("kind")!="pin" || pin->getString("mode")!="in" || !isNull(pin,"pads") || !isNull(pin,"source"))
      return fail("trigger and miso must be unbound input pins");
  }
  for (const char *name : {"cs","sck","mosi"}) {
    auto *pin=named[name];
    if (pin->getString("kind")!="pin" || pin->getString("mode")!="out" || !isNull(pin,"pads"))
      return fail("unsupported output pin binding");
  }
  auto *cs=named["cs"]->getObject("source");
  auto *reg=named["cs_n"], *init=reg->getObject("initial"), *value=reg->getObject("value");
  if (!cs || cs->getString("kind")!="name" || cs->getString("name")!="cs_n" ||
      reg->getString("kind")!="register" || !isNull(reg,"clock_enable") || !init ||
      init->getString("kind")!="integer" || init->getString("value")!="1" || !value ||
      value->getString("kind")!="unary" || value->getString("op")!="!" ||
      !field(value->getObject("value"),"bits","busy") ||
      !field(named["sck"]->getObject("source"),"sck_clock","out") ||
      !field(named["mosi"]->getObject("source"),"bits","out"))
    return fail("source outputs must be registered !bits.busy, sck_clock.out, and bits.out");
  auto *shift=named["bits"];
  auto *fields=shift->getArray("fields");
  llvm::json::Object *start=nullptr, *sample=nullptr;
  if (fields) for (auto &entry : *fields) {
    auto *f=entry.getAsObject();
    if (f && f->getString("name")=="start") start=f->getObject("value");
    if (f && f->getString("name")=="sample") sample=f->getObject("value");
  }
  if (!field(start,"transfer_control","action2")) return fail("shifter start must be transfer_control.action2");
  auto *sampleData=sample ? sample->getObject("left") : nullptr;
  if (!sampleData || sampleData->getString("kind")!="name" || sampleData->getString("name")!="miso")
    return fail("shifter sample must use the miso pin");
  if (named["transfer_control"]->getString("kind")!="protocol") return fail("invalid protocol declaration");
  std::string generated;
  llvm::raw_string_ostream stream(generated);
  if (deriveSpiComponent(module,"sck_clock","bits",txText,stream,false)) return 1;
  stream.flush();
  auto component=parseSourceString<ModuleOp>(generated,module.getContext());
  if (!component || failed(verify(*component))) return fail("derived SPI component failed verification");
  Operation *spi=findOne(*component,"spatial.spi");
  int64_t width,half,clock,tx;
  if (!spi || !integerAttr(spi,"width",width) || !integerAttr(spi,"half_period",half) ||
      !integerAttr(spi,"clock_hz",clock) || !integerAttr(spi,"tx_value",tx) ||
      width<9 || width>16 || clock>2*half*66000000LL)
    return fail("source component exceeds memory fabric width or provisional output-frequency estimate");
  std::vector<uint16_t> rows;
  if (mapControl(module,"transfer_control",nullptr,-1,{},&rows)) return 1;
  if (graphOnly) {
    // These memory milestones describe the existing RTL adapter and test
    // binding; source-level byte-stream semantics are not yet available.
    std::set<int> controlReachable{0};
    for (bool changed=true;changed;) {
      changed=false;
      auto snapshot=controlReachable;
      for (int state:snapshot) for (int inputs=0;inputs<4;++inputs)
        changed |= controlReachable.insert(rows[(state<<2)|inputs] & 3).second;
    }
    auto &o=graphOut;
    o << "module {\n  \"spatial.workload_graph\"() {\n    provenance = \"source-control-plus-rtl-adapter-v1\",\n"
      << "    memory_layout_origin = \"rtl-test-driver-not-source\",\n"
      << "    clock_hz = " << clock << " : i64, half_period = " << half << " : i64, control_rows = [";
    for (size_t i=0;i<rows.size();++i) o << (i ? ", " : "") << rows[i] << " : i64";
    o << "],\n    nodes = [\n"
      << "      {id = \"trigger\", opcode = \"input\", resource = \"input\", exclusive = true, inputs = [], outputs = [i1], duration = 1 : i64},\n"
      << "      {id = \"miso\", opcode = \"input\", resource = \"input\", exclusive = true, inputs = [], outputs = [i1], duration = 1 : i64},\n"
      << "      {id = \"read_hi\", opcode = \"read\", resource = \"memory\", group = \"transfer\", address = 100 : i64, inputs = [i1], outputs = [i8, i1], duration = 1 : i64},\n"
      << "      {id = \"read_lo\", opcode = \"read\", resource = \"memory\", group = \"transfer\", address = 101 : i64, inputs = [i1], outputs = [i8], duration = 1 : i64},\n"
      << "      {id = \"pack\", opcode = \"pack_big_endian_low_bits\", resource = \"adapter\", inputs = [i8, i8], outputs = [i" << width << ", i1], duration = 1 : i64},\n"
      << "      {id = \"control\", opcode = \"protocol\", resource = \"control\", states = " << controlReachable.size() << " : i64, control_inputs = 2 : i64, inputs = [i1, i1, i1], outputs = [i1], duration = 1 : i64},\n"
      << "      {id = \"timer\", opcode = \"timer\", resource = \"timer\", period = " << half << " : i64, inputs = [i1], outputs = [i1, i1, i1], duration = 1 : i64},\n"
      << "      {id = \"shift\", opcode = \"shift_msb_first\", resource = \"shifter\", inputs = [i" << width << ", i1, i1, i1, i1], outputs = [i" << width << ", i1, i1, i1], duration = 1 : i64},\n"
      << "      {id = \"unpack\", opcode = \"unpack_big_endian_low_bits\", resource = \"adapter\", inputs = [i" << width << "], outputs = [i8, i8], duration = 1 : i64},\n"
      << "      {id = \"write_hi\", opcode = \"write\", resource = \"memory\", group = \"transfer\", address = 200 : i64, inputs = [i8], outputs = [i1], duration = 1 : i64},\n"
      << "      {id = \"write_lo\", opcode = \"write\", resource = \"memory\", group = \"transfer\", address = 201 : i64, inputs = [i8, i1], outputs = [], duration = 1 : i64},\n"
      << "      {id = \"cs_invert\", opcode = \"invert\", resource = \"invert\", inputs = [i1], outputs = [i1], duration = 1 : i64},\n"
      << "      {id = \"mosi\", opcode = \"output\", resource = \"output\", exclusive = true, inputs = [i1], outputs = [], duration = 1 : i64},\n"
      << "      {id = \"sck\", opcode = \"output\", resource = \"output\", exclusive = true, inputs = [i1], outputs = [], duration = 1 : i64},\n"
      << "      {id = \"cs\", opcode = \"output\", resource = \"output\", exclusive = true, inputs = [i1], outputs = [], duration = 1 : i64}\n    ],\n    edges = [\n";
    struct E { const char *a; int ap; const char *b; int bp; const char *mode; };
    const E edges[]={ {"trigger",0,"control",0,"order"},{"trigger",0,"read_hi",0,"order"},{"read_hi",0,"pack",0,"data"},
      {"read_hi",1,"read_lo",0,"order"},{"read_lo",0,"pack",1,"data"},
      {"pack",1,"control",1,"data"},{"pack",0,"shift",0,"data"},
      {"control",0,"shift",1,"data"},{"timer",0,"shift",2,"data"},{"timer",1,"shift",3,"data"},{"miso",0,"shift",4,"data"},
      {"shift",1,"timer",0,"feedback"},{"shift",3,"control",2,"feedback"},{"shift",0,"unpack",0,"data"},
      {"unpack",0,"write_hi",0,"data"},{"unpack",1,"write_lo",0,"data"},
      {"write_hi",0,"write_lo",1,"order"},{"shift",2,"mosi",0,"data"},
      {"timer",2,"sck",0,"data"},{"shift",1,"cs_invert",0,"data"},{"cs_invert",0,"cs",0,"data"} };
    for (size_t i=0;i<sizeof(edges)/sizeof(edges[0]);++i) {
      const auto &e=edges[i];
      o << "      {from = \"" << e.a << "\", source_port = " << e.ap << " : i64, to = \"" << e.b
        << "\", target_port = " << e.bp << " : i64, mode = \"" << e.mode << "\"}"
        << (i+1==sizeof(edges)/sizeof(edges[0]) ? "\n" : ",\n");
    }
    o << "    ]\n  } : () -> ()\n}\n";
    return 0;
  }
  std::ofstream image(output);
  if (!image) return fail("cannot open memory SPI image");
  image << std::hex << std::setfill('0') << std::setw(4) << ((width<<8)|half) << '\n';
  image << "8000\n"; // CS selects !shift_busy; remaining selectors use index zero.
  for (uint16_t row : rows) image << std::hex << std::setfill('0') << std::setw(4) << row << '\n';
  image << std::hex << std::setfill('0') << std::setw(4) << (tx << (16-width)) << '\n';
  if (!image) return fail("cannot write memory SPI image");
  llvm::json::Object report{{"component","memory-spi-source-v1"},{"width",width},{"half_period",half},
      {"protocol_control_from_source",true},{"full_program_compiled",false},
      {"limitations","SRAM preload, address binding and trigger staging use the hand-written memory adapter; source streams/functions and cycle-accurate timing are not compiled."}};
  llvm::outs() << llvm::formatv("{0:2}\n",llvm::json::Value(std::move(report)));
  return 0;
}

#include "graph_mapper.inc"
#include "uart_lowering.inc"
#include "v2_images.inc"

// Route independent selectors, including explicit route.<sink> relays. This
// bounded exact search considers earlier choices when solving later demands;
// it does not yet place tiles or model link capacity/fanout/physical wires.
static int analyze(ModuleOp arch, ModuleOp program) {
  Operation *fabric = findOne(arch, "spatial.fabric");
  Operation *spi = findOne(program, "spatial.spi");
  Operation *frontend = spi ? nullptr : findOne(program, "spatial.frontend_program");
  if (!fabric || (!spi && !frontend)) return fail("expected spatial.fabric and a spatial.spi or spatial.frontend_program");
  auto name = fabric->getAttrOfType<StringAttr>("name");
  int64_t routeCycles;
  if (!integerAttr(fabric,"route_cycles",routeCycles) || routeCycles > 1000000)
    return fail("analysis route latency exceeds supported range");
  auto routes = fabric->getAttrOfType<ArrayAttr>("routes");
  std::vector<std::pair<std::string, std::string>> demands;
  int64_t ignoredStreams = 0;
  if (spi) {
    auto target = spi->getAttrOfType<StringAttr>("target");
    if (!target || target != name) return fail("program target does not match fabric");
    int64_t capacity, words, bits, states, inputs, clock, width, half, requiredWords, maxHz;
    if (!integerAttr(fabric,"shift_width",capacity) || !integerAttr(fabric,"storage_words",words) ||
        !integerAttr(fabric,"storage_word_bits",bits) || !integerAttr(fabric,"control_states",states) ||
        !integerAttr(fabric,"control_inputs",inputs) || !integerAttr(fabric,"pad_max_output_hz_estimate",maxHz) ||
        !integerAttr(spi,"width",width) || !integerAttr(spi,"half_period",half) ||
        !integerAttr(spi,"clock_hz",clock) || !integerAttr(spi,"storage_words_required",requiredWords))
      return fail("missing numeric analysis input");
    if (width > capacity || width > bits || requiredWords > words || states < 4 || inputs < 2)
      return fail("fabric resource capacity insufficient");
    if (clock > 1000000000 || half > 1000000000 || maxHz > 1000000000)
      return fail("analysis timing values exceed supported range");
    if (clock > 2 * half * maxHz) return fail("requested clock exceeds estimated pad output frequency");
    auto connections = spi->getAttrOfType<DictionaryAttr>("connections");
    for (NamedAttribute connection : connections)
      demands.emplace_back(connection.getName().strref().str(), cast<StringAttr>(connection.getValue()).getValue().str());
  } else {
    // This is a deliberately partial adapter from the real Rust frontend:
    // only simple, base-context single-signal Boolean dependencies. Do not
    // silently treat expressions, contexts, or byte streams as routed.
    std::set<std::string> sinkNames;
    for (Operation &op : frontend->getRegion(0).front()) {
      if (op.getName().getStringRef() == "spatial.frontend_stream") { ++ignoredStreams; continue; }
      if (op.getName().getStringRef() != "spatial.frontend_input") continue;
      auto json = llvm::json::parse(op.getAttrOfType<StringAttr>("input_json").getValue());
      if (!json) return fail("invalid frontend input JSON");
      auto *obj = json->getAsObject();
      auto *contextValue = obj->get("context");
      auto *check = obj->getObject("check");
      auto *expr = obj->getObject("expression");
      auto *refs = obj->getArray("references");
      if (!contextValue || !contextValue->getAsNull()) return fail("frontend contexts are not supported by Boolean dependency analysis");
      if (!check || check->getString("status") != "valid") return fail("frontend input is invalid or unresolved");
      if (!expr || (expr->getString("kind") != "name" && expr->getString("kind") != "field") || !refs || refs->size() != 1)
        return fail("only simple single-signal frontend inputs are supported");
      auto *ref = (*refs)[0].getAsObject();
      auto refName = ref ? ref->getString("name") : std::nullopt;
      auto signalKind = ref ? ref->getString("signal_kind") : std::nullopt;
      if (!refName || !signalKind || (*signalKind != "level" && *signalKind != "pulse"))
        return fail("frontend reference is not a resolved Boolean signal");
      std::string source = refName->str();
      if (auto output = ref->getString("output")) source += "." + output->str();
      std::string sink = op.getAttrOfType<StringAttr>("consumer").getValue().str() + "." +
                         op.getAttrOfType<StringAttr>("port").getValue().str();
      if (!sinkNames.insert(sink).second) return fail("duplicate frontend input sink");
      demands.emplace_back(std::move(sink), std::move(source));
    }
    if (demands.empty()) return fail("frontend has no supported Boolean dependencies");
  }
  if (routes.size() > 64 || demands.size() > 64) return fail("analysis graph exceeds 64-route limit");
  std::map<std::string, std::vector<std::string>> graph;
  for (Attribute entry : routes) {
    auto route = cast<DictionaryAttr>(entry);
    auto sink = route.getAs<StringAttr>("sink").getValue().str();
    auto sources = route.getAs<ArrayAttr>("sources");
    if (sources.size() > 64) return fail("selector exceeds 64-source limit");
    auto &options = graph[sink];
    for (Attribute source : sources) options.push_back(cast<StringAttr>(source).getValue().str());
  }
  for (const auto &entry : graph)
    for (const std::string &source : entry.second)
      if (llvm::StringRef(source).starts_with("route.") && !graph.count(source.substr(6)))
        return fail("route refers to missing relay " + source);
  std::map<std::string, size_t> selected;
  size_t searchSteps = 0;
  constexpr size_t maxSteps = 100000;
  std::function<bool(size_t)> solveDemands;
  std::function<bool(const std::string &, const std::string &, size_t, std::set<std::string>)> solvePath;
  solvePath = [&](const std::string &sink, const std::string &wanted, size_t demandIndex,
                  std::set<std::string> visiting) -> bool {
    if (++searchSteps > maxSteps || !visiting.insert(sink).second) return false;
    auto entry = graph.find(sink);
    if (entry == graph.end()) return false;
    auto assigned = selected.find(sink);
    size_t first = assigned == selected.end() ? 0 : assigned->second;
    size_t end = assigned == selected.end() ? entry->second.size() : first + 1;
    for (size_t choice = first; choice < end; ++choice) {
      const bool fresh = assigned == selected.end();
      if (fresh) selected[sink] = choice;
      const std::string &source = entry->second[choice];
      bool fits = source == wanted ? solveDemands(demandIndex + 1) :
                  llvm::StringRef(source).starts_with("route.") &&
                      solvePath(source.substr(6), wanted, demandIndex, visiting);
      if (fits) return true;
      if (fresh) selected.erase(sink);
      if (searchSteps > maxSteps) return false;
    }
    return false;
  };
  solveDemands = [&](size_t index) -> bool {
    if (index == demands.size()) return true;
    return solvePath(demands[index].first, demands[index].second, index, {});
  };
  if (!solveDemands(0))
    return fail(searchSteps > maxSteps ? "route search limit reached; feasibility unknown" : "no compatible selector assignment for requested connections");
  llvm::json::Array assignments;
  for (const auto &demand : demands) {
    std::string sink = demand.first;
    int64_t hops = 0;
    while (true) {
      ++hops;
      const std::string &source = graph.at(sink).at(selected.at(sink));
      if (source == demand.second) break;
      sink = source.substr(6);
    }
    assignments.push_back(llvm::json::Object{{"sink", demand.first}, {"source", demand.second},
        {"choice", static_cast<int64_t>(selected.at(demand.first))}, {"selector_hops", hops},
        {"selector_cycles", hops * routeCycles}});
  }
  llvm::json::Array selectedRoutes;
  for (const auto &choice : selected)
    selectedRoutes.push_back(llvm::json::Object{{"sink", choice.first},
        {"choice", static_cast<int64_t>(choice.second)}, {"source", graph.at(choice.first).at(choice.second)}});
  llvm::json::Object report{{"model", "selector-graph-analysis-v2"},
      {"target", name.getValue()}, {"assignments", std::move(assignments)},
      {"selected_routes", std::move(selectedRoutes)}, {"search_steps", static_cast<int64_t>(searchSteps)},
      {"frontend_dependency_only", frontend != nullptr}, {"unmodeled_word_streams", ignoredStreams},
      {"hardware_fidelity_verified", false}, {"image_emitted", false},
      {"limitations", "Boolean selector graph only; frontend mode routes named dependencies, not placed tiles; word streams are unmodeled. No resource fit, protocol proof, fanout/traffic capacity, deadlines, memory arbitration, RTL fidelity or physical timing proof."}};
  llvm::outs() << llvm::formatv("{0:2}\n", llvm::json::Value(std::move(report)));
  return 0;
}

int main(int argc, char **argv) {
  bool verifyOnly = argc == 3 && llvm::StringRef(argv[1]) == "--verify";
  bool frontendJson = argc == 3 && llvm::StringRef(argv[1]) == "--frontend-json";
  bool analyzeOnly = argc == 4 && llvm::StringRef(argv[1]) == "--analyze";
  bool controlOnly = argc == 5 && llvm::StringRef(argv[1]) == "--control-rows";
  bool fullControl = argc == 4 && llvm::StringRef(argv[1]) == "--compile-control";
  bool deriveSpi = argc == 6 && llvm::StringRef(argv[1]) == "--derive-spi-component";
  bool memorySpi = argc == 5 && llvm::StringRef(argv[1]) == "--compile-memory-spi";
  bool workloadGraph = argc == 3 && llvm::StringRef(argv[1]) == "--memory-spi-graph";
  bool uartGraph = argc == 3 && llvm::StringRef(argv[1]) == "--uart-graph";
  bool v2Spi = argc == 4 && llvm::StringRef(argv[1]) == "--compile-v2-spi";
  bool v2Uart = argc == 4 && llvm::StringRef(argv[1]) == "--compile-v2-uart";
  bool v2UartStream = argc == 4 && llvm::StringRef(argv[1]) == "--compile-v2-uart-stream";
  bool mapGraph = argc == 4 && llvm::StringRef(argv[1]) == "--map-graph";
  bool assessGraph = argc == 4 && llvm::StringRef(argv[1]) == "--assess-graph";
  if (!verifyOnly && !frontendJson && !analyzeOnly && !controlOnly && !fullControl && !deriveSpi && !memorySpi && !workloadGraph && !uartGraph && !v2Spi && !v2Uart && !v2UartStream && !mapGraph && !assessGraph && argc != 5)
    return fail("usage: spatial-circt-map --verify FILE | --frontend-json FILE | --analyze ARCH PROGRAM | --control-rows FRONTEND NAME OUTPUT | --compile-control FRONTEND OUTPUT | --derive-spi-component FRONTEND TIMER SHIFTER PAYLOAD | --compile-memory-spi FRONTEND PAYLOAD OUTPUT | --memory-spi-graph FRONTEND | --uart-graph FRONTEND | --compile-v2-spi FRONTEND OUTPUT | --compile-v2-uart FRONTEND OUTPUT | --compile-v2-uart-stream FRONTEND OUTPUT | --map-graph TARGET WORKLOAD | --assess-graph TARGET WORKLOAD | ARCH PROGRAM CONTROL_FSM OUTPUT.mem");
  DialectRegistry registry;
  registry.insert<circt::hw::HWDialect, circt::comb::CombDialect, circt::fsm::FSMDialect>();
  registerSpatialDialect(registry);
  MLIRContext context(registry);
  if (v2Spi || v2Uart || v2UartStream) {
    auto frontend=parseSourceFile<ModuleOp>(argv[2],&context);
    if (!frontend || failed(verify(*frontend))) return fail("invalid frontend MLIR");
    return compileV2Image(*frontend,argv[3],v2Uart || v2UartStream,v2UartStream);
  }
  if (uartGraph) {
    auto frontend=parseSourceFile<ModuleOp>(argv[2],&context);
    if (!frontend || failed(verify(*frontend))) return fail("invalid frontend MLIR");
    return lowerUart(*frontend);
  }
  if (mapGraph || assessGraph) {
    auto target = parseSourceFile<ModuleOp>(argv[2], &context);
    auto workload = parseSourceFile<ModuleOp>(argv[3], &context);
    if (!target || !workload || failed(verify(*target)) || failed(verify(*workload))) return fail("invalid graph MLIR");
    return mapWorkloadGraph(*target,*workload,assessGraph);
  }
  if (workloadGraph) {
    auto frontend = parseSourceFile<ModuleOp>(argv[2], &context);
    if (!frontend || failed(verify(*frontend))) return fail("invalid frontend MLIR");
    return compileMemorySpi(*frontend,"0",nullptr,true);
  }
  if (memorySpi) {
    auto frontend = parseSourceFile<ModuleOp>(argv[2], &context);
    if (!frontend || failed(verify(*frontend))) return fail("invalid frontend MLIR");
    return compileMemorySpi(*frontend,argv[3],argv[4]);
  }
  if (deriveSpi) {
    auto frontend = parseSourceFile<ModuleOp>(argv[2], &context);
    if (!frontend || failed(verify(*frontend))) return fail("invalid frontend MLIR");
    return deriveSpiComponent(*frontend, argv[3], argv[4], argv[5]);
  }
  if (fullControl) {
    auto frontend = parseSourceFile<ModuleOp>(argv[2], &context);
    if (!frontend || failed(verify(*frontend))) return fail("invalid frontend MLIR");
    return compileControl(*frontend, argv[3]);
  }
  if (controlOnly) {
    auto frontend = parseSourceFile<ModuleOp>(argv[2], &context);
    if (!frontend || failed(verify(*frontend))) return fail("invalid frontend MLIR");
    return mapControl(*frontend, argv[3], argv[4]);
  }
  if (verifyOnly || frontendJson) {
    auto module = parseSourceFile<ModuleOp>(argv[2], &context);
    if (!module || failed(verify(*module))) return fail("invalid MLIR input");
    if (frontendJson) return exportFrontend(*module);
    module->print(llvm::outs()); llvm::outs() << "\n";
    return 0;
  }
  if (analyzeOnly) {
    auto arch = parseSourceFile<ModuleOp>(argv[2], &context);
    auto program = parseSourceFile<ModuleOp>(argv[3], &context);
    if (!arch || !program || failed(verify(*arch)) || failed(verify(*program))) return fail("invalid MLIR input");
    return analyze(*arch, *program);
  }
  auto arch = parseSourceFile<ModuleOp>(argv[1], &context);
  auto program = parseSourceFile<ModuleOp>(argv[2], &context);
  auto control = parseSourceFile<ModuleOp>(argv[3], &context);
  if (!arch || !program || !control || failed(verify(*arch)) || failed(verify(*program)) || failed(verify(*control)))
    return fail("invalid MLIR input");
  Operation *fabric = findOne(*arch, "spatial.fabric");
  Operation *spi = findOne(*program, "spatial.spi");
  if (!fabric || !spi) return fail("expected exactly one spatial.fabric and one spatial.spi");
  auto machine = dyn_cast_or_null<circt::fsm::MachineOp>(findOne(*control, "fsm.machine"));
  if (!machine || machine.getInitialState() != "IDLE" || machine.getBody().empty())
    return fail("expected CIRCT fsm.machine with initial IDLE state");
  Block &fsmBody = machine.getBody().front();
  if (fsmBody.getNumArguments() != 2 || !fsmBody.getArgument(0).getType().isInteger(1) ||
      !fsmBody.getArgument(1).getType().isInteger(1)) return fail("FSM needs trigger and done i1 inputs");
  const char *stateNames[] = {"IDLE", "START", "TRANSFER", "FINISHED"};
  const char *nextNames[] = {"START", "TRANSFER", "FINISHED", "START"};
  const int expectedGuard[] = {0, -1, 1, 0};
  const int nextState[] = {1, 2, 3, 1};
  int guardInput[4];
  unsigned stateIndex = 0;
  for (Operation &raw : fsmBody.getOperations()) {
    auto state = dyn_cast<circt::fsm::StateOp>(raw);
    if (!state || stateIndex >= 4 || state.getSymName() != stateNames[stateIndex] ||
        !state.getOutput().empty() || state.getTransitions().empty())
      return fail("unsupported FSM state or output region");
    Block &transitions = state.getTransitions().front();
    if (!llvm::hasSingleElement(transitions)) return fail("exactly one transition per state is supported");
    auto transition = dyn_cast<circt::fsm::TransitionOp>(transitions.front());
    if (!transition || transition.getNextState() != nextNames[stateIndex] || !transition.getAction().empty())
      return fail("unsupported FSM transition or action");
    guardInput[stateIndex] = -1;
    if (!transition.getGuard().empty()) {
      Block &guard = transition.getGuard().front();
      if (!llvm::hasSingleElement(guard)) return fail("only direct input guards are supported");
      Operation &ret = guard.front();
      if (ret.getName().getStringRef() != "fsm.return" || ret.getNumOperands() != 1)
        return fail("unsupported FSM guard");
      auto arg = dyn_cast<BlockArgument>(ret.getOperand(0));
      if (!arg || arg.getOwner() != &fsmBody || arg.getArgNumber() > 1)
        return fail("FSM guard must reference trigger or done");
      guardInput[stateIndex] = static_cast<int>(arg.getArgNumber());
    }
    if (guardInput[stateIndex] != expectedGuard[stateIndex]) return fail("FSM guard does not match supported SPI mapping");
    ++stateIndex;
  }
  if (stateIndex != 4) return fail("expected four FSM states");
  auto name = fabric->getAttrOfType<StringAttr>("name");
  auto target = spi->getAttrOfType<StringAttr>("target");
  if (!name || !target || name.getValue() != "spi-fabric-v1" || target != name)
    return fail("target does not match the supported spi-fabric-v1 ABI");
  int64_t maxWidth, storageWords, storageBits, states, inputs, routeCycles;
  int64_t width, half, tx, clock, requestedWords, maxOutputHz, padRoundtrip;
  if (!integerAttr(fabric,"shift_width",maxWidth) || !integerAttr(fabric,"storage_words",storageWords) ||
      !integerAttr(fabric,"storage_word_bits",storageBits) || !integerAttr(fabric,"control_states",states) ||
      !integerAttr(fabric,"control_inputs",inputs) || !integerAttr(fabric,"route_cycles",routeCycles) ||
      !integerAttr(fabric,"pad_max_output_hz_estimate",maxOutputHz) ||
      !integerAttr(fabric,"pad_roundtrip_ns_estimate",padRoundtrip) ||
      !integerAttr(spi,"width",width) || !integerAttr(spi,"half_period",half) ||
      !integerAttr(spi,"clock_hz",clock) || !integerAttr(spi,"tx_value",tx) ||
      !integerAttr(spi,"storage_words_required",requestedWords))
    return fail("missing or non-i64 numeric attribute");
  // This backend encodes one exact RTL ABI; reject hypothetical incompatible
  // changes rather than pretending they correspond to existing hardware.
  if (maxWidth != 16 || storageWords != 4 || storageBits != 16 || states != 4 || inputs != 2 || routeCycles != 1)
    return fail("architecture capacities do not match the implemented prototype ABI");
  if (width < 1 || width > maxWidth || half < 8 || half > 255 || clock <= 0 || clock > 1000000000 ||
      tx < 0 || tx >= (int64_t(1) << width) || requestedWords != 2 || requestedWords > storageWords)
    return fail("invalid width, value, timing, or storage requirement (half-period must be >= 8)");
  if (spi->getNumOperands() != 0 || spi->getNumResults() != 1 ||
      !spi->getResult(0).getType().isSignlessInteger(static_cast<unsigned>(width)))
    return fail("SPI result type must be a signless integer matching its width");
  if (maxOutputHz <= 0 || maxOutputHz > 1000000000 || padRoundtrip <= 0 || padRoundtrip > 1000)
    return fail("invalid provisional pad estimates");
  if (clock > 2 * half * maxOutputHz) return fail("requested clock exceeds estimated pad output frequency");
  auto routes = fabric->getAttrOfType<ArrayAttr>("routes");
  auto connections = spi->getAttrOfType<DictionaryAttr>("connections");
  if (!routes || routes.size() != 8 || !connections || connections.size() != 8)
    return fail("exactly eight declared and requested routes required");
  const char *expected[] = {"start","run","rise","fall","input","mosi","sck","cs"};
  const char *required[] = {"control.start","shift.busy","timer.rise","timer.fall",
                            "pin.miso","shift.out","timer.level","control.not_selected"};
  const char *physical[8][4] = {
      {"control.start", "zero", "trigger", "timer.rise"},
      {"shift.busy", "control.selected", "zero", "one"},
      {"timer.rise", "timer.fall", "zero", "trigger"},
      {"timer.fall", "timer.rise", "zero", "trigger"},
      {"pin.miso", "zero", "one", "shift.out"},
      {"shift.out", "timer.level", "zero", "one"},
      {"timer.level", "shift.out", "zero", "one"},
      {"control.not_selected", "control.selected", "zero", "one"}};
  uint16_t selector = 0;
  for (unsigned i = 0; i < 8; ++i) {
    auto route = dyn_cast<DictionaryAttr>(routes[i]);
    if (!route) return fail("route must be a dictionary");
    auto sink = route.getAs<StringAttr>("sink");
    auto sources = route.getAs<ArrayAttr>("sources");
    if (!sink || sink.getValue() != expected[i] || !sources || sources.size() != 4)
      return fail("route sink ordering or source count disagrees with RTL ABI");
    auto wanted = connections.getAs<StringAttr>(sink.getValue());
    if (!wanted || wanted.getValue() != required[i])
      return fail("this backend only supports the verified SPI route assignment");
    int found = -1;
    std::set<std::string> choices;
    for (unsigned j = 0; j < 4; ++j) {
      auto src = dyn_cast<StringAttr>(sources[j]);
      if (!src || !choices.insert(src.getValue().str()).second) return fail("invalid or duplicate route source");
      if (src.getValue() != physical[i][j]) return fail("route source ordering does not match prototype RTL");
      if (src == wanted) found = static_cast<int>(j);
    }
    if (found < 0) return fail(("unroutable sink " + sink.getValue()).str());
    selector |= static_cast<uint16_t>(found << (2 * i));
  }
  // RTL route input indices are fixed wiring; accepting reordered architecture
  // choices would emit a valid-looking but wrong bitstream. Verify exact ABI.
  if (selector != 0) return fail("SPI route indices do not match prototype RTL");
  std::vector<uint16_t> words(19);
  words[0] = static_cast<uint16_t>((width << 8) | half);
  words[1] = selector;
  for (int row = 0; row < 16; ++row) {
    int state = row >> 2, trigger = (row >> 1) & 1, done = row & 1;
    bool transition = guardInput[state] < 0 || (guardInput[state] == 0 ? trigger : done);
    int next = transition ? nextState[state] : state;
    // This narrow SPI lowering supplies the prototype's registered actions;
    // the CIRCT FSM file currently defines state transitions, not actions.
    int selected = (next == 1 || next == 2) ? 1 : 0;
    int start = (state == 1 && next == 2) ? 1 : 0;
    int finished = next == 3 ? 1 : 0;
    int value = next | (selected << 2) | (start << 3) | (finished << 4);
    words[row + 2] = static_cast<uint16_t>(value);
  }
  words[18] = static_cast<uint16_t>(tx << (16 - width));
  std::ofstream image(argv[4]);
  if (!image) return fail("cannot open output image");
  for (auto word : words) image << std::hex << std::setfill('0') << std::setw(4) << word << '\n';
  if (!image) return fail("cannot write output image");
  // Construct registered MLIR output, making the emitted ABI inspectable.
  auto out = ModuleOp::create(UnknownLoc::get(&context));
  OpBuilder builder(&context);
  builder.setInsertionPointToStart(out.getBody());
  for (unsigned i = 0; i < 18; ++i) {
    OperationState op(builder.getUnknownLoc(), "spatial.config_write");
    op.addAttribute("address", builder.getI64IntegerAttr(i));
    op.addAttribute("value", builder.getI64IntegerAttr(words[i]));
    builder.create(op);
  }
  OperationState payload(builder.getUnknownLoc(), "spatial.payload");
  payload.addAttribute("slot", builder.getI64IntegerAttr(0));
  payload.addAttribute("value", builder.getI64IntegerAttr(words[18]));
  builder.create(payload);
  if (failed(verify(out))) return fail("invalid generated configuration operations");
  out.print(llvm::outs()); llvm::outs() << "\n";
  llvm::errs() << "Mapped SPI: " << width << " bits, " << clock / (2 * half)
               << " Hz nominal; provisional pad limits " << maxOutputHz << " Hz / "
               << padRoundtrip << " ns round trip; no pad timing proof.\n";
  return 0;
}
