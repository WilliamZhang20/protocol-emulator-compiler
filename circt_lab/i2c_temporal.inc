// Restricted lowering of the verified I2C write source profile to the grid's
// generic temporal sequencer. Reject source constructs outside this profile.
static llvm::json::Value stripSourceSpans(const llvm::json::Value &v) {
  if (auto *o = v.getAsObject()) {
    llvm::json::Object out;
    for (const auto &p : *o)
      if (p.first != "source_span") out[p.first] = stripSourceSpans(p.second);
    return std::move(out);
  }
  if (auto *a = v.getAsArray()) {
    llvm::json::Array out;
    for (const auto &x : *a) out.push_back(stripSourceSpans(x));
    return std::move(out);
  }
  return v;
}

static int lowerI2cWrite(ModuleOp module, const char *profilePath,
                         llvm::StringRef addressText, llvm::StringRef payloadText) {
  Operation *root = findOne(module, "spatial.frontend_program");
  if (!root) return fail("expected one frontend program");
  auto profileBuffer = llvm::MemoryBuffer::getFile(profilePath);
  if (!profileBuffer) return fail("cannot read I2C source profile");
  auto profile = llvm::json::parse((*profileBuffer)->getBuffer());
  if (!profile || !profile->getAsArray()) return fail("invalid I2C source profile");
  std::vector<llvm::json::Value> items;
  for (Operation &op : root->getRegion(0).front()) {
    if (op.getName().getStringRef() != "spatial.frontend_item") continue;
    auto parsed = llvm::json::parse(op.getAttrOfType<StringAttr>("item_json").getValue());
    if (!parsed) return fail("invalid source item");
    items.push_back(stripSourceSpans(*parsed));
  }
  int64_t halfPeriod = -1;
  for (const auto &expected : *profile->getAsArray()) {
    auto *e = expected.getAsObject();
    if (!e) return fail("invalid I2C profile item");
    auto kind = e->getString("kind"), name = e->getString("name");
    llvm::json::Value *actual = nullptr;
    for (auto &item : items) {
      auto *o = item.getAsObject();
      if (o && o->getString("kind") == kind && o->getString("name") == name) {
        if (actual) return fail("ambiguous I2C source item");
        actual = &item;
      }
    }
    if (!actual) return fail("I2C source is missing a required item");
    if (kind == "constant" && name == "HALF") {
      auto *value = actual->getAsObject()->getObject("value");
      auto text = value ? value->getString("value") : std::nullopt;
      if (!text || text->getAsInteger(10, halfPeriod) || halfPeriod < 2 || halfPeriod > 65535)
        return fail("HALF must be 2..65535 cycles");
      (*value)["value"] = "250";
    }
    if (kind == "resource" && (name == "byte_clock" || name == "cond_clock")) {
      auto *timings = actual->getAsObject()->getArray("timings");
      auto *first = timings && timings->size() == 1 ? (*timings)[0].getAsArray() : nullptr;
      auto *timing = first && first->size() == 2 ? (*first)[1].getAsObject() : nullptr;
      auto number = timing ? timing->getString("numerator") : std::nullopt;
      int64_t resolved = -1;
      if (!number || number->getAsInteger(10, resolved) || resolved != halfPeriod)
        return fail("timer period must resolve to HALF");
      (*timing)["numerator"] = "250";
    }
    if (*actual != expected)
      return fail(("I2C source item does not match supported write profile: " +
                   (name ? name->str() : kind->str())).c_str());
  }
  unsigned address = 0;
  if (addressText.getAsInteger(0, address) || address > 127)
    return fail("I2C address must be a 7-bit integer");
  if (payloadText.size() % 2 || payloadText.size() > 64)
    return fail("payload must contain at most 32 complete hex bytes");
  std::vector<unsigned> bytes{address << 1};
  for (size_t i = 0; i < payloadText.size(); i += 2) {
    unsigned byte = 0;
    if (payloadText.substr(i, 2).getAsInteger(16, byte)) return fail("invalid payload hex");
    bytes.push_back(byte);
  }
  auto row=[](int halt,int no,int yes,int event,int action) {
    return (halt<<15)|(no<<11)|(yes<<7)|(event<<4)|action;
  };
  // Moore actions: SDA, SCL, frame-start/reset and context. Event 5 is the
  // sampled ACK, event 6 frame completion, event 7 stream empty.
  llvm::json::Array rows{row(0,1,1,0,7), row(0,1,2,1,2), row(0,2,3,1,0),
    row(0,4,4,0,4), row(0,5,5,0,12), row(0,5,6,6,8), row(0,7,8,5,8),
    row(0,4,8,7,8), row(0,9,9,0,4), row(0,9,10,1,0), row(0,10,11,1,2),
    row(0,11,12,1,3), row(0,13,14,5,3), row(1,0,0,0,3),
    row(1,0,0,0,7), row(1,0,0,0,7)};
  llvm::json::Array memory;
  for (unsigned byte : bytes) {
    unsigned frame = (byte << 1) | 1;
    memory.push_back(frame >> 8); memory.push_back(frame & 255);
  }
  auto length = static_cast<int64_t>(bytes.size() * 2);
  llvm::json::Object words{{"0",halfPeriod},{"1",halfPeriod},{"2",41},{"3",2},
    {"4",91},{"41",1},{"53",1},{"54",1},{"55",1},{"63",10},
    {"64",0},{"65",0},{"66",length},{"67",length},{"68",3},
    {"88",0},{"89",100},{"90",200}};
  llvm::json::Object result{{"entry",0},{"idle_actions",3},{"rows",std::move(rows)},
    {"base_words",std::move(words)},{"payload_base",100},{"payload_bytes",std::move(memory)},
    {"source_function","write_only"},{"source_profile","i2c-write-v1"},
    {"full_source_program_compiled",false},
    {"limitations","Specialized write-only profile; repeated start, reads and other functions are not lowered."}};
  llvm::outs() << llvm::formatv("{0:2}\n", llvm::json::Value(std::move(result)));
  return 0;
}
