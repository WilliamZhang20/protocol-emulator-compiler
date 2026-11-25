// Fixed semantic profiles lower a coded USB data packet or the Ethernet
// source's stored-frame fragment onto the common streaming coder and CRC.
static int lowerCodedStream(ModuleOp module, const char *profilePath,
                            llvm::StringRef mode, llvm::StringRef payloadText) {
  if (mode!="usb" && mode!="ethernet" && mode!="ethernet_session") return fail("coded mode must be usb, ethernet or ethernet_session");
  Operation *root=findOne(module,"spatial.frontend_program");
  if (!root) return fail("expected one frontend program");
  auto file=llvm::MemoryBuffer::getFile(profilePath);
  if (!file) return fail("cannot read coded-stream profile");
  auto profile=llvm::json::parse((*file)->getBuffer());
  if (!profile || !profile->getAsArray()) return fail("invalid coded-stream profile");
  std::vector<llvm::json::Value> items;
  for (Operation &op:root->getRegion(0).front()) {
    if (op.getName().getStringRef()!="spatial.frontend_item") continue;
    auto parsed=llvm::json::parse(op.getAttrOfType<StringAttr>("item_json").getValue());
    if (!parsed) return fail("invalid source item");
    items.push_back(stripSourceSpans(*parsed));
  }
  if (mode=="ethernet_session") {
    if (!payloadText.empty()) return fail("Ethernet packet comes from source WIRE_FRAME");
    return lowerEthernetSession(items,*profile);
  }
  for (const auto &expected:*profile->getAsArray()) {
    auto *e=expected.getAsObject(); if (!e) return fail("invalid profile item");
    const llvm::json::Value *actual=nullptr;
    for (auto &item:items) {
      auto *o=item.getAsObject();
      if (o && o->getString("kind")==e->getString("kind") && o->getString("name")==e->getString("name")) {
        if (actual) return fail("ambiguous source item"); actual=&item;
      }
    }
    if (!actual || *actual!=expected) return fail("source item differs from supported coded-stream profile");
  }
  std::vector<unsigned> bytes;
  if (mode=="usb") {
    if (payloadText.size()%2 || payloadText.size()<4 || payloadText.size()>20) return fail("USB low-speed DATA0 requires 0..8 data bytes plus SYNC and PID");
    for (size_t i=0;i<payloadText.size();i+=2) {
      unsigned v=0; if (payloadText.substr(i,2).getAsInteger(16,v)) return fail("invalid USB packet hex");bytes.push_back(v);
    }
    if (bytes[0]!=0x80 || bytes[1]!=0xc3) return fail("USB profile requires low-speed SYNC and DATA0 PID");
  } else {
    if (!payloadText.empty()) return fail("Ethernet packet comes from source WIRE_FRAME");
    const llvm::json::Object *constant=nullptr;
    for (auto &item:items) {auto *o=item.getAsObject();if(o && o->getString("kind")=="constant" && o->getString("name")=="WIRE_FRAME") {
      if (constant) return fail("duplicate WIRE_FRAME");constant=o;}}
    auto *value=constant ? constant->getObject("value") : nullptr;
    auto *values=value ? value->getArray("values") : nullptr;
    if (!value || value->getString("kind")!="list" || !values || values->size()<68 || values->size()>1522)
      return fail("WIRE_FRAME must contain preamble and 60..1514 frame bytes");
    for (auto &entry:*values) {
      auto *o=entry.getAsObject();auto s=o ? o->getString("value") : std::nullopt;unsigned v=0;
      if (!o || o->getString("kind")!="integer" || !s || s->getAsInteger(10,v) || v>255)
        return fail("WIRE_FRAME must contain literal bytes");
      bytes.push_back(v);
    }
    for (unsigned i=0;i<7;i++) if(bytes[i]!=0x55) return fail("unsupported Ethernet preamble");
    if (bytes[7]!=0xd5) return fail("unsupported Ethernet SFD");
  }
  int period=mode=="usb" ? 33 : 2;
  int poly=mode=="usb" ? 0x8005 : 0x04c11db7;
  int shape=(mode=="usb" ? 15 : 31)|0x60;
  int coder=mode=="usb" ? 0x201e : 0xc000;
  int skip=mode=="usb" ? 1024 : 4096;
  llvm::json::Object words{{"0",period},{"1",period},{"3",5},{"4",0x103},{"41",1},
    {"63",5},{"64",0},{"65",0},{"66",static_cast<int64_t>(bytes.size())},{"67",0},{"68",0},
    {"69",poly&65535},{"70",poly>>16},{"71",0},{"72",0},{"73",0},{"74",0},{"75",shape},
    {"76",0},{"88",0},{"89",100},{"90",200},{"93",1},{"117",7},{"118",1},
    {"119",2048},{"120",96},{"121",coder},{"122",skip},{"123",mode=="usb"?3:0}};
  llvm::json::Array payload;for(auto byte:bytes)payload.push_back(byte);
  llvm::json::Object result{{"base_words",std::move(words)},{"payload_base",100},{"payload_bytes",std::move(payload)},
    {"source_profile",mode=="usb"?"usb-low-speed-data0":"ethernet-stored-frame"},
    {"full_source_function_compiled",mode=="usb"},
    {"limitations",mode=="usb"?"Fixed DATA0 transmit profile; no receive or host enumeration.":"Frame fragment only; link pulses and analog PHY are not implemented."}};
  auto row=[](int h,int no,int yes,int ev,int act){return (h<<15)|(no<<11)|(yes<<7)|(ev<<4)|act;};
  llvm::json::Array rows;
  int idle=0;
  if (mode=="usb") {
    rows=llvm::json::Array{row(0,0,1,6,4),row(0,1,2,1,12),row(0,2,3,1,12),row(0,3,4,1,14)};
    for(int i=4;i<16;i++)rows.push_back(row(1,0,0,0,10));
    idle=10;
  } else {
    // Stay quiet until the first byte is actually launched, then expose the
    // differential stream and return both outputs low at the final edge.
    rows=llvm::json::Array{row(0,0,1,2,8),row(0,1,2,6,0)};
    for(int i=2;i<16;i++)rows.push_back(row(1,0,0,0,8));
    idle=8;
  }
  result["temporal"]=llvm::json::Object{{"entry",0},{"idle_actions",idle},{"rows",std::move(rows)}};
  llvm::outs()<<llvm::formatv("{0:2}\n",llvm::json::Value(std::move(result)));
  return 0;
}
