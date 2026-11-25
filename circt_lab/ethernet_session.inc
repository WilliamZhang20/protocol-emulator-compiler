// Ethernet 10BASE-T link session: the whole exported transmit() of the
// stored-frame source (link pulses, frame, end-of-frame idle) compiled onto
// the 5x3 grid. Declarations must match the profile exactly (NLP_TICK and the
// frame bytes are parameters); the session body is compiled per statement.
//
// Resource binding (all generic tiles, no Ethernet-specific hardware):
//   cadence  -> timer0, free running at NLP_TICK
//   half_bit -> timer1, started by bits.send and stopped before each count
//   pulse    -> timer2, started by `fire link_pulse`, stopped by its own fall
//   tail     -> timer3, started by the idle window, stopped by its own tick
//   repeat N { wait next cadence.tick } -> shifter0 as an N-tick counter
//   idle_high, positive, negative + tail start -> four registered LUT4 cells
// The two pin contexts collapse into the LUT4 output functions: positive is
// bits.out while busy, otherwise idle_high or the link pulse. This is exact
// because the body compiler admits `fire link_pulse` only while `sending` is
// clear and the frame (and its idle) only while it is set.
static const llvm::json::Object *sessionField(const llvm::json::Object &item, llvm::StringRef name) {
  if (auto *fields=item.getArray("fields"))
    for (auto &f:*fields) if (auto *o=f.getAsObject(); o && o->getString("name")==name) return o->getObject("value");
  return nullptr;
}

static bool sessionCycles(const llvm::json::Object *v, unsigned &out) {
  if (!v || v->getString("kind")!="quantity") return false;
  auto unit=v->getString("unit"), text=v->getString("value");
  return unit && (*unit=="cycles" || *unit=="cycle") && text && !text->getAsInteger(10,out) && out>=1 && out<=65535;
}

static bool isCall(const llvm::json::Object &s, llvm::StringRef callee, llvm::StringRef object="") {
  auto *e=s.getObject("expression"); if (!e || e->getString("kind")!="call") return false;
  auto *c=e->getObject("callee"); if (!c) return false;
  if (object.empty()) return c->getString("kind")=="name" && c->getString("name")==callee;
  auto *o=c->getObject("object");
  return c->getString("kind")=="field" && c->getString("field")==callee && o && o->getString("name")==object;
}

static bool isWait(const llvm::json::Object &s, llvm::StringRef object, llvm::StringRef field, bool next) {
  auto *sig=s.getObject("signal"); auto *o=sig ? sig->getObject("object") : nullptr;
  return s.getString("op")=="wait" && s.getBoolean("next")==next && sig && sig->getString("kind")=="field" &&
         sig->getString("field")==field && o && o->getString("name")==object;
}

static int lowerEthernetSession(const std::vector<llvm::json::Value> &items, const llvm::json::Value &profile) {
  // Session-specific source parameters.
  unsigned nlpTick=0; const llvm::json::Object *transmit=nullptr, *wire=nullptr;
  for (auto &item:items) {
    auto *o=item.getAsObject(); if (!o) return fail("invalid source item");
    auto kind=o->getString("kind"), name=o->getString("name");
    if (kind=="test") continue;
    if (kind=="constant" && name=="NLP_TICK") { if (!sessionCycles(o->getObject("value"),nlpTick)) return fail("NLP_TICK must be 1..65535 cycles"); continue; }
    if (kind=="constant" && name=="WIRE_FRAME") { wire=o; continue; }
    if (kind=="constant" && name=="FRAME") continue;
    if (kind=="function" && name=="transmit") { transmit=o; continue; }
    bool listed=false;
    for (auto &e:*profile.getAsArray()) { auto *p=e.getAsObject();
      if (p->getString("kind")==kind && p->getString("name")==name && (name || e==item)) listed=true; }
    if (!listed) return fail("source item outside the Ethernet session profile");
  }
  if (!nlpTick || !transmit || !wire) return fail("Ethernet session needs NLP_TICK, WIRE_FRAME and transmit()");
  for (const auto &expected:*profile.getAsArray()) {
    auto *e=expected.getAsObject(); const llvm::json::Value *actual=nullptr;
    if (!e->getString("name")) {  // unnamed items (contexts) must appear verbatim
      bool found=false; for (auto &item:items) if (item==expected) found=true;
      if (!found) return fail("source item differs from supported Ethernet session profile");
      continue;
    }
    for (auto &item:items) { auto *o=item.getAsObject();
      if (o->getString("kind")==e->getString("kind") && o->getString("name")==e->getString("name")) { if (actual) return fail("ambiguous source item"); actual=&item; } }
    if (!actual) return fail("source item missing from Ethernet session");
    llvm::json::Value copy=*actual;
    if (e->getString("name")=="cadence") {  // cadence runs every NLP_TICK
      auto *t=copy.getAsObject()->getArray("timings");
      auto *entry=t && t->size()==1 ? (*t)[0].getAsArray() : nullptr;
      auto *q=entry && entry->size()==2 ? (*entry)[1].getAsObject() : nullptr;
      if (!q || q->getString("numerator")!=std::to_string(nlpTick)) return fail("cadence must run every NLP_TICK");
      (*q)["numerator"]="NLP_TICK";
    }
    if (copy!=expected) return fail("source item differs from supported Ethernet session profile");
  }
  if (transmit->getBoolean("exported")!=true || !transmit->getArray("params") || !transmit->getArray("params")->empty())
    return fail("transmit() must be exported and take no parameters");
  std::vector<unsigned> bytes;
  auto *values=wire->getObject("value") ? wire->getObject("value")->getArray("values") : nullptr;
  if (!values || values->size()<68 || values->size()>1522) return fail("WIRE_FRAME must contain preamble and 60..1514 frame bytes");
  for (auto &entry:*values) { auto *o=entry.getAsObject(); auto s=o ? o->getString("value") : std::nullopt; unsigned v=0;
    if (!o || o->getString("kind")!="integer" || !s || s->getAsInteger(10,v) || v>255) return fail("WIRE_FRAME must contain literal bytes");
    bytes.push_back(v); }
  for (unsigned i=0;i<7;i++) if (bytes[i]!=0x55) return fail("unsupported Ethernet preamble");
  if (bytes[7]!=0xd5) return fail("unsupported Ethernet SFD");

  // Timer parameters come from the (profile-checked) declarations.
  unsigned halfBit=0,pulseEvery=0,pulseFirst=0,tailEvery=0,tailFirst=0;
  for (auto &item:items) { auto *o=item.getAsObject(); auto n=o->getString("name");
    if (o->getString("kind")!="resource" || !n) continue;
    if (*n=="half_bit" && !sessionCycles(sessionField(*o,"every"),halfBit)) return fail("half_bit period");
    if (*n=="pulse" && (!sessionCycles(sessionField(*o,"every"),pulseEvery) || !sessionCycles(sessionField(*o,"first"),pulseFirst))) return fail("pulse timing");
    if (*n=="tail" && (!sessionCycles(sessionField(*o,"every"),tailEvery) || !sessionCycles(sessionField(*o,"first"),tailFirst))) return fail("tail timing"); }
  // The source counts TP_IDL from bits.done through its registered pins; on
  // the grid the idle window and the timer start each add a registered stage.
  const unsigned tailPipeline=1;
  if (tailFirst<=tailPipeline) return fail("tail first is shorter than the grid idle pipeline");

  // Compile the session body. Actions: bit0 fires the pulse timer, bit1
  // starts the tick counter (and parks half_bit), bit2 starts half_bit, which
  // releases the armed frame on its first tick. Events: 0 always, 3 pulse.fall,
  // 4 tail.tick (routed sequencer inputs), 5 counter done, 6 packet stop.
  auto row=temporalRow;
  std::vector<TemporalRow> rows; bool sending=false, sent=false, idleOpen=false, pulsePending=false; int repeatCount=-1;
  auto emitStep=[&](int act){int n=rows.size();rows.push_back(row(0,n+1,n+1,0,act));};
  auto emitWait=[&](int ev){int n=rows.size();rows.push_back(row(0,n,n+1,ev,0));};
  for (auto &stmt:*transmit->getArray("body")) {
    auto *s=stmt.getAsObject(); if (!s) return fail("invalid statement");
    auto op=s->getString("op");
    if (op=="evaluate" && isCall(*s,"clear_flags")) {
      if (idleOpen) return fail("clear_flags before the frame idle has ended (wait tail.tick)");
      sending=false; continue; }
    if (op=="evaluate" && isCall(*s,"set_flags")) {
      auto *args=s->getObject("expression")->getArray("args");
      auto *v=args && args->size()==1 ? (*args)[0].getAsObject()->getObject("value") : nullptr;
      if (!v || v->getString("name")!="sending") return fail("only set_flags(sending) is supported");
      sending=true; continue; }
    if (op=="repeat") {
      auto *count=s->getObject("count"); auto *body=s->getArray("body"); unsigned n=0;
      auto text=count ? count->getString("value") : std::nullopt;
      if (!count || count->getString("kind")!="integer" || !text || text->getAsInteger(10,n) || n<1 || n>16 ||
          !body || body->size()!=1 || !(*body)[0].getAsObject() || !isWait(*(*body)[0].getAsObject(),"cadence","tick",true))
        return fail("only repeat 1..16 { wait next cadence.tick } is supported");
      if (repeatCount>=0 && repeatCount!=(int)n) return fail("all cadence repeats must use the same count (one counter width)");
      if (pulsePending || idleOpen) return fail("wait for pulse.fall / tail.tick before counting cadence ticks");
      repeatCount=n; emitStep(2); emitWait(5); continue; }
    if (op=="fire" && s->getString("event")=="link_pulse") {
      if (sending) return fail("link pulses are only compiled while sending is clear");
      emitStep(1); pulsePending=true; continue; }
    if (op=="wait" && isWait(*s,"pulse","fall",false)) {
      if (!pulsePending) return fail("wait pulse.fall without a pending link pulse");
      emitWait(3); pulsePending=false; continue; }
    if (op=="fire" && s->getString("event")=="begin_frame") continue;  // the packet start restarts FCS
    if (op=="evaluate" && isCall(*s,"send","bits")) {
      auto *args=s->getObject("expression")->getArray("args");
      auto *v=args && args->size()==1 ? (*args)[0].getAsObject()->getObject("value") : nullptr;
      if (!v || v->getString("name")!="WIRE_FRAME") return fail("bits.send must send WIRE_FRAME");
      if (!sending || sent || pulsePending) return fail("one frame, sent with sending set and no pulse pending");
      emitStep(4); sent=true; idleOpen=true; continue; }
    if (op=="wait" && isWait(*s,"tail","tick",false)) {
      if (!idleOpen) return fail("wait tail.tick without a frame");
      emitWait(4); idleOpen=false; continue; }
    return fail("statement outside the compiled Ethernet session subset");
  }
  if (!sent || idleOpen || pulsePending || sending) return fail("session must send one frame and end idle with flags clear");
  rows.push_back(row(1,0,0,0,0));
  int64_t rowsUsed=rows.size();

  // Registered LUT4 tables, index {d,c,b,a}.
  auto table=[](auto f){int t=0;for(int i=0;i<16;i++) if(f(i&1,(i>>1)&1,(i>>2)&1,(i>>3)&1)) t|=1<<i; return t;};
  int positive=table([](int busy,int out,int idle,int pulse){return busy ? out : (idle|pulse);});
  int negative=table([](int busy,int out,int,int){return busy && !out;});
  int window=table([](int idle,int busy,int tick,int){return idle && !busy && !tick;});
  int idleHigh=table([](int busy,int idle,int tick,int){return busy || (idle && !tick);});
  int width=repeatCount<1 ? 1 : repeatCount;
  llvm::json::Object words{
    {"0",(int64_t)nlpTick},{"1",(int64_t)nlpTick},{"2",width|0x20},{"3",0},{"4",0x103},{"41",1},{"47",1},
    {"53",(int64_t)halfBit},{"54",(int64_t)halfBit},
    {"63",5},{"64",0},{"65",0},{"66",static_cast<int64_t>(bytes.size())},{"67",0},{"68",0},
    {"69",0x04c11db7&65535},{"70",0x04c11db7>>16},{"71",0},{"72",0},{"73",0},{"74",0},{"75",31|0x60},
    {"76",0},{"88",0},{"89",100},{"90",200},{"93",1},{"117",7},{"118",1},
    {"119",2048},{"120",96},{"121",0xc000},{"122",4096},{"123",0},
    {"152",positive},{"153",negative},{"154",window},{"155",idleHigh},
    {"172",(int64_t)pulseFirst},{"173",(int64_t)pulseEvery},
    {"174",(int64_t)(tailFirst-tailPipeline)},{"175",(int64_t)tailEvery},{"184",3}};
  llvm::json::Array payload; for (auto b:bytes) payload.push_back(b);
  llvm::json::Object result{{"base_words",std::move(words)},{"payload_base",100},{"payload_bytes",std::move(payload)},
    {"source_profile","ethernet-10base-t-link-session"},{"full_source_function_compiled",true},
    {"nlp_tick_cycles",(int64_t)nlpTick},{"link_pulse_period_cycles",(int64_t)nlpTick*width},
    {"sequencer_rows_used",rowsUsed},
    {"limitations","Digital 10BASE-T transmit waveforms only: no analog PHY/magnetics, receive, collision or auto-negotiation."}};
  // No stretchable rows yet: a longer session needs its link-pulse timing
  // reviewed before page loads may delay any phase.
  result["temporal"]=temporalProgram(rows,0,0,{});
  llvm::outs()<<llvm::formatv("{0:2}\n",llvm::json::Value(std::move(result)));
  return 0;
}
