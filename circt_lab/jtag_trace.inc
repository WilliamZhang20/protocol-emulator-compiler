// Lower a bounded, static JTAG source test into SRAM frames.  The source
// profile validates the semantics of step/scan8 and the grid component.
static int lowerJtagTest(ModuleOp module, const char *profilePath, llvm::StringRef requested) {
  Operation *root = findOne(module, "spatial.frontend_program");
  if (!root) return fail("expected one frontend program");
  auto file = llvm::MemoryBuffer::getFile(profilePath);
  if (!file) return fail("cannot read JTAG source profile");
  auto profile = llvm::json::parse((*file)->getBuffer());
  if (!profile || !profile->getAsArray()) return fail("invalid JTAG profile");
  std::vector<llvm::json::Value> items;
  for (Operation &op : root->getRegion(0).front()) {
    if (op.getName().getStringRef() != "spatial.frontend_item") continue;
    auto parsed = llvm::json::parse(op.getAttrOfType<StringAttr>("item_json").getValue());
    if (!parsed) return fail("invalid source item");
    items.push_back(stripSourceSpans(*parsed));
  }
  for (const auto &expected : *profile->getAsArray()) {
    auto *e=expected.getAsObject(); if (!e) return fail("invalid JTAG profile item");
    const llvm::json::Value *actual=nullptr;
    for (const auto &item : items) {
      auto *o=item.getAsObject();
      if (o && o->getString("kind")==e->getString("kind") && o->getString("name")==e->getString("name")) {
        if (actual) return fail("ambiguous JTAG source item");
        actual=&item;
      }
    }
    if (!actual || *actual!=expected) return fail("JTAG source differs from supported component profile");
  }
  const llvm::json::Object *test=nullptr;
  for (auto &item : items) {
    auto *o=item.getAsObject();
    if (o && o->getString("kind")=="test" && o->getString("name")==requested) {
      if (test) return fail("duplicate JTAG source test");
      test=o;
    }
  }
  auto *body=test ? test->getArray("body") : nullptr;
  if (!body) return fail("JTAG test not found");
  std::vector<unsigned> tms,tdi;
  llvm::json::Array checks,peerChecks;
  bool attached=false;
  for (const auto &entry : *body) {
    auto *stmt=entry.getAsObject(); if (!stmt) return fail("invalid JTAG statement");
    auto op=stmt->getString("op");
    if (op=="attach") {
      auto *peer=stmt->getObject("peer"); auto *callee=peer ? peer->getObject("callee") : nullptr;
      auto *args=peer ? peer->getArray("args") : nullptr;
      if (attached || !callee || callee->getString("name")!="jtag_tap" || !args || args->size()!=4)
        return fail("requires exactly one JTAG peer");
      const char *names[]={"tck","tms","tdi","tdo"};
      for (unsigned i=0;i<4;i++) {
        auto *a=(*args)[i].getAsObject(); auto *v=a ? a->getObject("value") : nullptr;
        if (!a || a->getString("name")!=names[i] || !v || v->getString("name")!=names[i])
          return fail("unsupported JTAG peer binding");
      }
      attached=true; continue;
    }
    const llvm::json::Object *call=nullptr; int expectedValue=-1;
    if (op=="evaluate") call=stmt->getObject("expression");
    else if (op=="expect") {
      auto *cond=stmt->getObject("condition"); auto *right=cond ? cond->getObject("right") : nullptr;
      auto *values=right ? right->getArray("values") : nullptr;
      auto *v=values && values->size()==1 ? (*values)[0].getAsObject() : nullptr;
      auto n=v ? v->getString("value") : std::nullopt; unsigned parsed=0;
      if (!cond || cond->getString("kind")!="binary" || cond->getString("op")!="==" ||
          !stmt->get("fails") || !stmt->get("fails")->getAsNull() || !right || right->getString("kind")!="list" ||
          !v || v->getString("kind")!="integer" || !n || n->getAsInteger(10,parsed) || parsed>255)
        return fail("unsupported JTAG expectation");
      expectedValue=parsed; call=cond->getObject("left");
    } else return fail("unsupported JTAG test statement");
    auto *callee=call ? call->getObject("callee") : nullptr;
    auto *args=call ? call->getArray("args") : nullptr;
    // Simulator-only peer.read assertions check the attached TAP model at
    // this boundary. They are not synthesized into target hardware.
    if (callee && callee->getString("kind")=="field" && callee->getString("field")=="read") {
      auto *obj=callee->getObject("object");
      auto *arg=args && args->size()==1 ? (*args)[0].getAsObject() : nullptr;
      auto *v=arg ? arg->getObject("value") : nullptr;
      if (expectedValue<0 || !obj || obj->getString("kind")!="name" || obj->getString("name")!="tap" ||
          !arg || !arg->get("name") || !arg->get("name")->getAsNull() || !v ||
          v->getString("kind")!="integer" || v->getString("value")!="1")
        return fail("unsupported JTAG peer assertion");
      peerChecks.push_back(llvm::json::Object{{"bit_offset",static_cast<int64_t>(tms.size())},{"value",expectedValue}});
      continue;
    }
    auto fn=callee ? callee->getString("name") : std::nullopt;
    if (!call || call->getString("kind")!="call" || !args || args->size()!=2 ||
        !fn || (*fn!="step" && *fn!="scan8")) return fail("unsupported JTAG call");
    unsigned width=*fn=="step" ? 1 : 8, values[2];
    for (unsigned i=0;i<2;i++) {
      auto *a=(*args)[i].getAsObject(); auto *v=a ? a->getObject("value") : nullptr;
      auto text=v ? v->getString("value") : std::nullopt;
      if (!a || !a->get("name") || !a->get("name")->getAsNull() || !v ||
          v->getString("kind")!="integer" || !text || text->getAsInteger(10,values[i]) || values[i]>=(1u<<width))
        return fail("JTAG arguments must be literal values fitting the call width");
    }
    if (expectedValue>=0) {
      if (static_cast<unsigned>(expectedValue)>=(1u<<width)) return fail("expectation exceeds scan width");
      checks.push_back(llvm::json::Object{{"bit_offset",static_cast<int64_t>(tms.size())},
                                          {"width",width},{"value",expectedValue}});
    }
    for (unsigned bit=0;bit<width;bit++) {tms.push_back((values[0]>>bit)&1);tdi.push_back((values[1]>>bit)&1);}
  }
  if (!attached || tms.size()<5 || tms.size()>384 || !std::all_of(tms.begin(),tms.begin()+5,[](unsigned x){return x==1;}))
    return fail("JTAG workload must begin with five reset clocks and fit in 384 clocks");
  // Track the TAP state to permit padding only in Run-Test/Idle; otherwise
  // extra clocks may modify an external device and must not be emitted.
  const int next[16][2]={{1,0},{1,2},{3,9},{4,5},{4,5},{6,8},{6,7},{4,8},{1,2},
                         {10,0},{11,12},{11,12},{13,15},{13,14},{11,15},{1,2}};
  int state=0; for (unsigned bit:tms) state=next[state][bit];
  auto sourceCycles=tms.size();
  if (tms.size()%8 && state!=1) return fail("cannot pad JTAG clocks outside idle");
  while (tms.size()%8) {tms.push_back(0);tdi.push_back(0);}
  llvm::json::Array payload;
  for (size_t offset=0;offset<tms.size();offset+=8) {
    unsigned d=0,m=0; for (unsigned bit=0;bit<8;bit++) {d|=tdi[offset+bit]<<bit;m|=tms[offset+bit]<<bit;}
    payload.push_back(d);payload.push_back(m);
  }
  auto row=[](int h,int no,int yes,int ev,int act){return (h<<15)|(no<<11)|(yes<<7)|(ev<<4)|act;};
  // Request a paired shifter frame; wait for its SRAM write; repeat until
  // the generic transmit stream is empty, then halt without an error.
  llvm::json::Array rows{row(0,1,1,0,12),row(0,1,2,6,8),row(0,0,3,7,8)};
  for(int i=3;i<16;i++)rows.push_back(row(1,0,0,0,8));
  llvm::json::Object temporal{{"entry",0},{"idle_actions",8},{"rows",std::move(rows)}};
  llvm::json::Object result{{"temporal",std::move(temporal)},{"source_test",requested},{"payload_bytes",std::move(payload)},
    {"checks",std::move(checks)},{"peer_checks",std::move(peerChecks)},{"source_cycles",static_cast<int64_t>(sourceCycles)},
    {"padding_idle_cycles",static_cast<int64_t>(tms.size()-sourceCycles)},
    {"rx_capacity",static_cast<int64_t>(tms.size()/8)},
    {"limitations","Static test with literal step/scan8 calls; idle padding adds clocks. Not a general JTAG program compiler."}};
  llvm::outs()<<llvm::formatv("{0:2}\n",llvm::json::Value(std::move(result)));
  return 0;
}
