// Whole exported source functions compiled onto the grid's generic temporal
// sequencer. Declarations must match a profile (named constants may be
// parameters); each function body is compiled statement by statement and any
// statement outside the supported subset is rejected.

// Match declarations against a profile. Timer timings whose profile numerator
// names a parameter constant must resolve to that constant's value.
static int matchTemporalProfile(const std::vector<llvm::json::Value> &items, const llvm::json::Value &profile,
                                const std::map<std::string,unsigned> &params,
                                const std::set<std::string> &compiledFunctions, const char *what) {
  for (auto &item:items) {
    auto *o=item.getAsObject(); if (!o) return fail("invalid source item");
    auto kind=o->getString("kind"), name=o->getString("name");
    if (kind=="test") continue;
    if (kind=="function" && name && compiledFunctions.count(name->str())) continue;
    bool listed=false;
    for (auto &e:*profile.getAsArray()) { auto *p=e.getAsObject();
      if (p && p->getString("kind")==kind && p->getString("name")==name && (name || e==item)) listed=true; }
    if (!listed) return fail((std::string("source item outside the ")+what+" profile: "+(name ? name->str() : kind ? kind->str() : "?")).c_str());
  }
  for (const auto &expected:*profile.getAsArray()) {
    auto *e=expected.getAsObject(); if (!e) return fail("invalid profile item");
    if (!e->getString("name")) {
      bool found=false; for (auto &item:items) if (item==expected) found=true;
      if (!found) return fail((std::string("source item differs from the ")+what+" profile").c_str());
      continue;
    }
    const llvm::json::Value *actual=nullptr;
    for (auto &item:items) { auto *o=item.getAsObject();
      if (o->getString("kind")==e->getString("kind") && o->getString("name")==e->getString("name")) {
        if (actual) return fail("ambiguous source item"); actual=&item; } }
    if (!actual) return fail((std::string("source item missing: ")+e->getString("name")->str()).c_str());
    llvm::json::Value copy=*actual;
    auto *c=copy.getAsObject();
    if (c->getString("kind")=="constant" && params.count(c->getString("name")->str())) {
      if (auto *v=c->getObject("value")) (*v)["value"]=c->getString("name")->str();
    }
    const llvm::json::Array *want=e->getArray("timings"); llvm::json::Array *have=c->getArray("timings");
    if (want && have && want->size()==have->size())
      for (size_t i=0;i<want->size();++i) {
        const llvm::json::Array *we=(*want)[i].getAsArray(); llvm::json::Array *he=(*have)[i].getAsArray();
        const llvm::json::Object *wq=we && we->size()==2 ? (*we)[1].getAsObject() : nullptr;
        auto *hq=he && he->size()==2 ? (*he)[1].getAsObject() : nullptr;
        auto wn=wq ? wq->getString("numerator") : std::nullopt;
        if (!wq || !hq || !wn || !params.count(wn->str())) continue;
        if (hq->getString("numerator")==std::to_string(params.at(wn->str()))) (*hq)["numerator"]=wn->str();
      }
    if (copy!=expected) return fail((std::string("source item differs from the ")+what+" profile: "+e->getString("name")->str()).c_str());
  }
  return 0;
}

static bool temporalCycles(const llvm::json::Object &item, unsigned &out) {
  auto *v=item.getObject("value");
  if (!v || v->getString("kind")!="quantity") return false;
  auto unit=v->getString("unit"), text=v->getString("value");
  return unit && (*unit=="cycles" || *unit=="cycle") && text && !text->getAsInteger(10,out) && out>=1 && out<=65535;
}

static const llvm::json::Object *findItem(const std::vector<llvm::json::Value> &items, llvm::StringRef kind, llvm::StringRef name) {
  for (auto &item:items) if (auto *o=item.getAsObject(); o && o->getString("kind")==kind && o->getString("name")==name) return o;
  return nullptr;
}

static bool isFieldRef(const llvm::json::Object *v, llvm::StringRef object, llvm::StringRef field) {
  auto *o=v ? v->getObject("object") : nullptr;
  return v && v->getString("kind")=="field" && v->getString("field")==field && o && o->getString("kind")=="name" && o->getString("name")==object;
}

static int lut4Table(const std::function<int(int,int,int,int)> &f) {
  int t=0; for (int i=0;i<16;i++) if (f(i&1,(i>>1)&1,(i>>2)&1,(i>>3)&1)) t|=1<<i; return t;
}

// SPI mode-0 controller: the whole exported transfer() of examples/spi.spt.
//
// Resource binding (generic tiles only):
//   sck_clock -> timer0, bits -> shifter0 (one SRAM byte per word)
//   cs_off    -> timer1 (started by `fire finish`, stopped by its own tick)
//   gap       -> timer2 (started by cs_off.tick, stopped by its own tick)
//   reg selected -> LUT4 cell 0 (begin || selected && !cs_off.tick)
//   reg cs_n     -> LUT4 cell 1 holding `selected`; the pin takes its
//                   complement output, so cs_n resets to its declared 1
// The source's `open` register and `sent` counter start one word after
// another until data.len words have begun. The sequencer lowers that loop
// directly: it requests one frame per word until the host-supplied length is
// exhausted (event 7), so the counter is not instantiated.
// Sequencer actions: bit0 `begin`, bit1 `finish`, bits 2+3 request a frame.
// Events: 0 always, 3 cs_off.tick and 4 gap.tick (routed inputs), 6 frame
// done, 7 transfer length exhausted.
static int lowerSpiTransfer(const std::vector<llvm::json::Value> &items, const llvm::json::Value &profile,
                            const std::vector<unsigned> &example) {
  auto *halfItem=findItem(items,"constant","HALF"); unsigned half=0;
  if (!halfItem || !temporalCycles(*halfItem,half) || half<2) return fail("HALF must be 2..65535 cycles");
  if (int rc=matchTemporalProfile(items,profile,{{"HALF",half}},{"transfer"},"SPI transfer")) return rc;
  auto *fn=findItem(items,"function","transfer");
  if (!fn || fn->getBoolean("exported")!=true) return fail("transfer() must be exported");
  auto *params=fn->getArray("params");
  auto *param=params && params->size()==1 ? (*params)[0].getAsArray() : nullptr;
  auto *ptype=param && param->size()==2 ? (*param)[1].getAsObject() : nullptr;
  auto *ret=fn->getObject("returns");
  if (!ptype || (*param)[0].getAsString()!="data" || ptype->getString("name")!="bytes" || ptype->getString("via")!="bits" ||
      !ret || ret->getString("name")!="bytes" || ret->getString("via")!="bits")
    return fail("transfer must take data: bytes via bits and return bytes via bits");
  unsigned gapCycles=0;
  if (auto *gap=findItem(items,"resource","gap")) {
    auto *t=gap->getArray("timings"); auto *e=t && t->size()==1 ? (*t)[0].getAsArray() : nullptr;
    auto *q=e && e->size()==2 ? (*e)[1].getAsObject() : nullptr;
    auto n=q ? q->getString("numerator") : std::nullopt;
    if (!n || n->getAsInteger(10,gapCycles) || gapCycles<1 || gapCycles>65535 || q->getString("denominator")!="1") return fail("gap period");
  }

  std::vector<TemporalRow> rows; bool limited=false, begun=false, words=false, finished=false, csOff=false, gapped=false;
  auto step=[&](int act){int n=rows.size();rows.push_back(temporalRow(0,n+1,n+1,0,act));};
  auto wait=[&](int ev){int n=rows.size();rows.push_back(temporalRow(0,n,n+1,ev,0));};
  for (auto &stmt:*fn->getArray("body")) {
    auto *s=stmt.getAsObject(); if (!s) return fail("invalid statement");
    auto op=s->getString("op");
    if (op=="assign") {
      auto *target=s->getObject("target"), *value=s->getObject("value");
      auto *right=value ? value->getObject("right") : nullptr;
      if (!isFieldRef(target,"sent","limit") || !value || value->getString("op")!="-" ||
          !isFieldRef(value->getObject("left"),"data","len") || !right || right->getString("value")!="1" || limited || begun)
        return fail("only `sent.limit = data.len - 1` before `fire begin` is supported");
      limited=true; continue;
    }
    if (op=="fire" && s->getString("event")=="begin") {
      if (!limited || begun) return fail("fire begin must follow the word limit, once");
      step(1); begun=true; continue;
    }
    if (op=="repeat") {
      auto *body=s->getArray("body"); auto *w=body && body->size()==1 ? (*body)[0].getAsObject() : nullptr;
      if (!isFieldRef(s->getObject("count"),"data","len") || !w || w->getString("op")!="wait" ||
          w->getBoolean("next")!=false || !isFieldRef(w->getObject("signal"),"bits","word"))
        return fail("only `repeat data.len { wait bits.word }` is supported");
      if (!begun || words) return fail("the word loop must follow fire begin, once");
      int request=rows.size();
      step(12);                                   // request one frame (one word)
      wait(6);                                    // that word's frame finishes
      int n=rows.size();                          // more words? (length exhausted)
      rows.push_back(temporalRow(0,request,n+1,7,0));
      words=true; continue;
    }
    if (op=="fire" && s->getString("event")=="finish") {
      if (!words || finished) return fail("fire finish must follow the word loop, once");
      step(2); finished=true; continue;
    }
    if (op=="wait" && s->getBoolean("next")==false && isFieldRef(s->getObject("signal"),"cs_off","tick")) {
      if (!finished || csOff) return fail("wait cs_off.tick must follow fire finish");
      wait(3); csOff=true; continue;
    }
    if (op=="wait" && s->getBoolean("next")==false && isFieldRef(s->getObject("signal"),"gap","tick")) {
      if (!csOff || gapped) return fail("wait gap.tick must follow cs_off.tick");
      wait(4); gapped=true; continue;
    }
    return fail("statement outside the compiled SPI transfer subset");
  }
  if (!gapped) return fail("transfer must end CS (wait cs_off.tick) and its gap (wait gap.tick)");
  rows.push_back(temporalRow(1,0,0,0,0));
  int64_t used=rows.size();

  int selected=lut4Table([](int begin,int sel,int tick,int){return begin || (sel && !tick);});
  int csReg=lut4Table([](int sel,int,int,int){return sel;});
  int64_t length=example.size();
  llvm::json::Object words_{
    {"0",(int64_t)half},{"1",(int64_t)half},{"2",8|0x20},{"3",0},{"4",7},
    {"53",(int64_t)half},{"54",(int64_t)half},{"55",1},
    {"63",5},{"64",0},{"65",0},{"66",length},{"67",length},{"68",0},
    {"69",0},{"70",0},{"71",0},{"72",0},{"73",0},{"74",0},{"75",0},{"76",0},
    {"88",0},{"89",100},{"90",200},{"93",0},{"123",0},
    {"152",selected},{"153",csReg},{"172",(int64_t)gapCycles},{"173",(int64_t)gapCycles},{"184",3}};
  llvm::json::Array payload; for (auto b:example) payload.push_back((int64_t)b);
  llvm::json::Object result{{"base_words",std::move(words_)},{"payload_base",100},{"payload_bytes",std::move(payload)},
    {"reply_base",200},{"source_profile","spi-mode0-transfer"},{"source_function","transfer"},
    {"full_source_function_compiled",true},{"half_period_cycles",(int64_t)half},{"gap_cycles",(int64_t)gapCycles},
    {"sequencer_rows_used",used},
    {"limitations","One SRAM byte per word: consecutive words are separate frames, so SCK pauses between words (no edges while paused)."}};
  // No stretchable rows: CS, SCK and the gap are timed by the fabric.
  result["temporal"]=temporalProgram(rows,0,0,{});
  // How a host runs transfer(data) on the chip: the generic runner in the
  // hardware repo (test/cocotb_tests/test_compiled.py) interprets this.
  auto len=[]{return llvm::json::Object{{"len","data"}};};
  result["interface"]=llvm::json::Object{
    {"pins",llvm::json::Object{{"cs","uo0"},{"sck","uio5"},{"mosi","uio6"},{"miso","uio0"}}},
    {"functions",llvm::json::Object{{"transfer",llvm::json::Object{
      {"words",llvm::json::Object{{"66",len()},{"67",len()},{"89",100},{"90",200}}},
      {"sram",llvm::json::Array{llvm::json::Object{{"base",100},{"bytes",llvm::json::Object{{"arg","data"}}}}}},
      {"reply",llvm::json::Object{{"base",200},{"count",len()}}}}}}}};
  llvm::outs()<<llvm::formatv("{0:2}\n",llvm::json::Value(std::move(result)));
  return 0;
}

// Source expression -> the runner's argument language (see the hardware
// repo's test_compiled.py `evaluate`). Names must be parameters or loop items.
static bool argExpr(const llvm::json::Object *e, const std::set<std::string> &names, llvm::json::Value &out) {
  if (!e) return false;
  auto kind=e->getString("kind");
  if (kind=="integer") { int64_t v=0; auto t=e->getString("value"); if (!t || t->getAsInteger(10,v)) return false; out=v; return true; }
  if (kind=="name") { auto n=e->getString("name"); if (!n || !names.count(n->str())) return false; out=llvm::json::Object{{"arg",n->str()}}; return true; }
  if (kind=="binary") {
    auto op=e->getString("op"); llvm::json::Value a=nullptr,b=nullptr;
    if (!op || (*op!="<<" && *op!="|" && *op!="+" && *op!="-" && *op!="&" && *op!=">>")) return false;
    if (!argExpr(e->getObject("left"),names,a) || !argExpr(e->getObject("right"),names,b)) return false;
    out=llvm::json::Object{{"op",op->str()},{"a",std::move(a)},{"b",std::move(b)}}; return true;
  }
  return false;
}

static llvm::json::Value binop(const char *op, llvm::json::Value a, llvm::json::Value b) {
  return llvm::json::Object{{"op",op},{"a",std::move(a)},{"b",std::move(b)}};
}

// I2C controller: the whole exported write_read() of examples/i2c.spt.
//
// Each write()/read() call is one frame from SRAM: 16-bit (byte << 1 | ninth)
// for SDA (shifter0, 9 bits MSB first, sampled on observed SCL rises) and one
// tag byte for shifter1, which runs for the whole frame. A registered LUT4
// cell latches the tag (busy ? tag : held) and drives sequencer event 3: it
// marks the last frame before restart(). START, restart() and stop() are the
// source's condition() patterns, played by the sequencer one HALF slot at a
// time in context 0 (actions drive SDA and SCL, the byte clock free-runs for
// slot timing); frames run in context 1 (byte clock on SCL, shifter on SDA,
// both frozen while a device stretches SCL). After each frame the program
// branches on: stream finished (stop, success), the sampled ninth bit (a
// NACK: stop, then fail "nack"), then the restart tag.
static int lowerI2cWriteRead(const std::vector<llvm::json::Value> &items, const llvm::json::Value &profile) {
  auto *halfItem=findItem(items,"constant","HALF"); unsigned half=0;
  if (!halfItem || !temporalCycles(*halfItem,half) || half<2) return fail("HALF must be 2..65535 cycles");
  if (int rc=matchTemporalProfile(items,profile,{{"HALF",half}},{"write_read"},"I2C write_read")) return rc;
  auto *fn=findItem(items,"function","write_read");
  if (!fn || fn->getBoolean("exported")!=true) return fail("write_read() must be exported");
  std::set<std::string> scalars, sequences;
  for (auto &p:*fn->getArray("params")) {
    auto *pair=p.getAsArray(); auto name=pair && pair->size()==2 ? (*pair)[0].getAsString() : std::nullopt;
    auto *type=pair && pair->size()==2 ? (*pair)[1].getAsObject() : nullptr;
    if (!name || !type) return fail("invalid parameter");
    (type->getString("name")=="bytes" ? sequences : scalars).insert(name->str());
  }
  auto callOf=[](const llvm::json::Object *s, llvm::StringRef callee)->const llvm::json::Array* {
    auto *e=s ? s->getObject("expression") : nullptr; auto *c=e ? e->getObject("callee") : nullptr;
    if (!s || s->getString("op")!="evaluate" || !e || e->getString("kind")!="call" || !c || c->getString("name")!=callee) return nullptr;
    return e->getArray("args");
  };
  auto readLast=[&](const llvm::json::Object *s,int &last)->bool {
    auto *args=callOf(s,"read"); auto *a=args && args->size()==1 ? (*args)[0].getAsObject() : nullptr;
    auto *v=a ? a->getObject("value") : nullptr; auto t=v ? v->getString("value") : std::nullopt;
    return a && a->getString("name")=="last" && v && v->getString("kind")=="integer" && t && (*t=="0" || *t=="1") && ((last=*t=="1"),true);
  };
  // Segments of frames, in order.
  struct Segment {std::string kind; llvm::json::Value value=nullptr; std::string loop; int ninth=1;
                  llvm::json::Value tag=0; bool tagLoopLast=false;};
  std::vector<Segment> segs; size_t sinceCondition=0; bool started=false, stopped=false, reading=false, finalRead=false;
  auto body=fn->getArray("body");
  for (size_t i=0;i<body->size();++i) {
    auto *s=(*body)[i].getAsObject(); if (!s) return fail("invalid statement");
    if (stopped) return fail("stop() must be the last statement");
    if (auto *a=callOf(s,"start"); a && a->empty()) { if (started) return fail("one start() only"); started=true; sinceCondition=segs.size(); continue; }
    if (!started) return fail("write_read must begin with start()");
    if (auto *a=callOf(s,"write"); a && a->size()==1) {
      if (reading) return fail("write() after a read is outside the compiled subset");
      llvm::json::Value v=nullptr;
      if (!argExpr((*a)[0].getAsObject()->getObject("value"),scalars,v)) return fail("unsupported write() argument");
      segs.push_back({"write",std::move(v)}); continue;
    }
    if (s->getString("op")=="for") {
      auto *it=s->getObject("iterable"); auto var=s->getString("variable"); auto *b=s->getArray("body");
      auto *w=b && b->size()==1 ? (*b)[0].getAsObject() : nullptr; auto *a=callOf(w,"write");
      if (!it || it->getString("kind")!="name" || !sequences.count(it->getString("name")->str()) || !var || !a || a->size()!=1)
        return fail("only `for x in <bytes parameter> { write(...) }` loops are supported");
      if (reading) return fail("write() after a read is outside the compiled subset");
      auto names=scalars; names.insert(var->str()); llvm::json::Value v=nullptr;
      if (!argExpr((*a)[0].getAsObject()->getObject("value"),names,v)) return fail("unsupported write() argument");
      Segment seg{"for",std::move(v),it->getString("name")->str()}; seg.value=llvm::json::Object{{"item",var->str()},{"value",std::move(seg.value)}};
      segs.push_back(std::move(seg)); continue;
    }
    if (auto *a=callOf(s,"restart"); a && a->empty()) {
      if (reading) return fail("restart() after a read is outside the compiled subset");
      // Tag the last frame before the restart: the last item of trailing loops,
      // or the fixed frame before them when every trailing loop is empty.
      llvm::json::Value allEmpty=1; bool fixed=false;
      for (size_t k=segs.size();k-->sinceCondition;) {
        auto &seg=segs[k];
        if (seg.kind=="for") { seg.tagLoopLast=true; seg.tag=allEmpty;
          allEmpty=binop("&",allEmpty,binop("==",llvm::json::Object{{"len",seg.loop}},0)); continue; }
        seg.tag=allEmpty; fixed=true; break;
      }
      if (!fixed) return fail("restart() needs a write() with a fixed argument before it");
      sinceCondition=segs.size(); continue;
    }
    if (s->getString("op")=="repeat") {
      auto *b=s->getArray("body"); auto *r=b && b->size()==1 ? (*b)[0].getAsObject() : nullptr; int last=0; llvm::json::Value count=nullptr;
      if (!r || !readLast(r,last) || last || !argExpr(s->getObject("count"),scalars,count))
        return fail("only `repeat <count> { read(last: 0) }` is supported");
      if (finalRead) return fail("read(last: 1) must be the final frame");
      reading=true; Segment seg{"repeat",std::move(count)}; seg.ninth=0; segs.push_back(std::move(seg)); continue;
    }
    if (int last=0; readLast(s,last)) {
      if (finalRead) return fail("read(last: 1) must be the final frame");
      if (!last) { Segment seg{"read",1}; seg.ninth=0; reading=true; segs.push_back(std::move(seg)); continue; }
      reading=finalRead=true; Segment seg{"read",1}; seg.ninth=1; segs.push_back(std::move(seg)); continue;
    }
    if (auto *a=callOf(s,"stop"); a && a->empty()) {
      if (!finalRead) return fail("the compiled subset ends with read(last: 1) then stop()");
      stopped=true; continue;
    }
    return fail("statement outside the compiled I2C write_read subset");
  }
  if (!stopped) return fail("write_read must end with stop()");

  // Frame bytes: [(x << 1 | ninth) >> 8, (x << 1 | ninth) & 255, tag].
  auto frame=[](llvm::json::Value x,int ninth,llvm::json::Value tag)->llvm::json::Array {
    auto word=binop("|",binop("<<",std::move(x),1),ninth);
    return llvm::json::Array{binop(">>",word,8),binop("&",word,255),std::move(tag)};
  };
  llvm::json::Array bytes; llvm::json::Value frames=0, beforeReads=0; bool readSeen=false;
  for (auto &seg:segs) {
    llvm::json::Value n=1;
    if (seg.kind=="write") bytes.push_back(frame(seg.value,1,seg.tag));
    else if (seg.kind=="for") {
      auto *o=seg.value.getAsObject(); std::string item=o->getString("item")->str();
      llvm::json::Value tag=seg.tagLoopLast ? binop("&",llvm::json::Object{{"arg","is_last"}},seg.tag) : llvm::json::Value(0);
      bytes.push_back(llvm::json::Object{{"last",seg.loop},{"as",item},{"bytes",frame(*o->get("value"),1,std::move(tag))}});
      n=llvm::json::Object{{"len",seg.loop}};
    } else if (seg.kind=="repeat") {
      bytes.push_back(llvm::json::Object{{"repeat",seg.value},{"bytes",frame(0xFF,0,0)}});
      n=seg.value;
    } else bytes.push_back(frame(0xFF,seg.ninth,0));
    if (seg.kind=="repeat" || seg.kind=="read") readSeen=true;
    else if (!readSeen) beforeReads=binop("+",beforeReads,n);
    frames=binop("+",frames,std::move(n));
  }
  llvm::json::Value readCount=binop("-",frames,beforeReads);

  auto row=temporalRow;
  // Actions: bit0 SDA, bit1 SCL (context 0), bit2 stops the slot clock,
  // bit3 selects context 1; 12 requests a frame. Events: 1 slot tick,
  // 3 restart tag, 5 sampled ninth bit, 6 frame done, 7 stream finished.
  std::vector<TemporalRow> rows{
    row(0,1,1,0,7),                    // 0 bus idle, slot clock reset
    row(0,1,2,1,2), row(0,2,3,1,0),    // 1-2 START tail: SDA low, then SCL low
    row(0,4,4,0,4),                    // 3 reset the clock for the frame
    row(0,5,5,0,12),                   // 4 request a frame
    row(0,5,6,6,8),                    // 5 wait for it
    row(0,7,9,7,8),                    // 6 stream finished -> stop (success)
    row(0,8,9,5,8),                    // 7 NACK -> stop (fail "nack")
    row(0,4,14,3,8),                   // 8 restart tag -> restart, else next frame
    row(0,9,10,1,0), row(0,10,11,1,2), // 9-10 stop(): SDA low/SCL low, SCL high
    row(0,13,12,7,3),                  // 11 SDA high; finished stream = success
    row(1,0,0,0,3), row(1,0,0,0,7),    // 12 done, 13 fail "nack" (error bit)
    row(0,14,15,1,1), row(0,15,1,1,3)};// 14-15 restart(): SDA high, SCL high, then START tail
  int latch=lut4Table([](int busy,int tag,int held,int){return busy ? tag : held;});
  const int64_t txBase=100, rxBase=600;
  llvm::json::Object words{
    {"0",(int64_t)half},{"1",(int64_t)half},{"2",9|0x20},{"3",2},{"4",91},{"41",1},
    {"53",1},{"54",1},{"55",1},
    {"63",2|(1<<2)|(1<<9)|(1<<13)},{"64",0},{"65",0},{"66",3},{"67",1},{"68",3},
    {"69",0},{"70",0},{"71",0},{"72",0},{"73",0},{"74",0},{"75",0},{"76",0},
    {"88",0},{"89",txBase},{"90",rxBase},{"93",0},{"123",0},{"154",latch},{"184",1}};
  llvm::json::Object result{{"base_words",std::move(words)},{"source_profile","i2c-write-read"},{"source_function","write_read"},
    {"full_source_function_compiled",true},{"half_period_cycles",(int64_t)half},{"sequencer_rows_used",16},
    {"limitations","Reads must end the transaction (read(last: 1) then stop()); writes after reads and restart() after a read are rejected."}};
  // Rows 1 (START hold), 3 (between frames), 9 (STOP: SCL low) and 14
  // (restart: SCL low) may start late: a controller may hold SCL low or hold
  // a START longer, and every phase they begin is timed from slot ticks.
  result["temporal"]=temporalProgram(rows,0,3,{1,3,9,14});
  result["interface"]=llvm::json::Object{
    {"pins",llvm::json::Object{{"scl","uio5"},{"sda","uio6"}}},
    {"functions",llvm::json::Object{{"write_read",llvm::json::Object{
      {"words",llvm::json::Object{{"66",binop("*",3,frames)},{"67",frames},{"89",txBase},{"90",rxBase}}},
      {"sram",llvm::json::Array{llvm::json::Object{{"base",txBase},{"bytes",std::move(bytes)}}}},
      {"reply",llvm::json::Object{{"base",binop("+",rxBase,beforeReads)},{"count",std::move(readCount)}}}}}}}};
  llvm::outs()<<llvm::formatv("{0:2}\n",llvm::json::Value(std::move(result)));
  return 0;
}

static int lowerTemporalFunction(ModuleOp module, const char *profilePath, llvm::StringRef function, llvm::StringRef arg) {
  Operation *root=findOne(module,"spatial.frontend_program");
  if (!root) return fail("expected one frontend program");
  auto file=llvm::MemoryBuffer::getFile(profilePath);
  if (!file) return fail("cannot read temporal function profile");
  auto profile=llvm::json::parse((*file)->getBuffer());
  if (!profile || !profile->getAsArray()) return fail("invalid temporal function profile");
  std::vector<llvm::json::Value> items;
  for (Operation &op:root->getRegion(0).front()) {
    if (op.getName().getStringRef()!="spatial.frontend_item") continue;
    auto parsed=llvm::json::parse(op.getAttrOfType<StringAttr>("item_json").getValue());
    if (!parsed) return fail("invalid source item");
    items.push_back(stripSourceSpans(*parsed));
  }
  std::vector<unsigned> bytes;
  if (arg.size()%2) return fail("example bytes must be whole hex bytes");
  for (size_t i=0;i<arg.size();i+=2) { unsigned b=0; if (arg.substr(i,2).getAsInteger(16,b)) return fail("invalid hex"); bytes.push_back(b); }
  if (function=="spi.transfer") {
    if (bytes.empty() || bytes.size()>1024) return fail("SPI example needs 1..1024 data bytes");
    return lowerSpiTransfer(items,*profile,bytes);
  }
  if (function=="i2c.write_read") {
    if (!bytes.empty()) return fail("I2C write_read takes its arguments at run time");
    return lowerI2cWriteRead(items,*profile);
  }
  return fail("unknown temporal function (expected spi.transfer or i2c.write_read)");
}
