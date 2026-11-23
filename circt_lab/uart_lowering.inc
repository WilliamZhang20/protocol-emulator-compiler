// Restricted UART source lowering. Emits an analysis-only typed graph.
// It resolves source bindings, verifies the supported behavior and fails closed.
static int lowerUart(ModuleOp module) {
  Operation *root=findOne(module,"spatial.frontend_program");
  if (!root) return fail("expected frontend program");
  using Obj=llvm::json::Object;
  std::vector<llvm::json::Value> items, streams;
  for (Operation &op:root->getRegion(0).front()) {
    auto kind=op.getName().getStringRef();
    if (kind=="spatial.frontend_item" || kind=="spatial.frontend_stream") {
      auto attr=op.getAttrOfType<StringAttr>(kind=="spatial.frontend_item" ? "item_json" : "stream_json");
      auto value=llvm::json::parse(attr.getValue());
      if (!value || !value->getAsObject()) return fail("invalid UART frontend JSON");
      (kind=="spatial.frontend_item" ? items : streams).push_back(std::move(*value));
    } else if (kind=="spatial.frontend_input") {
      auto v=llvm::json::parse(op.getAttrOfType<StringAttr>("input_json").getValue());
      auto *o=v ? v->getAsObject() : nullptr; auto *check=o ? o->getObject("check") : nullptr;
      if (!check || check->getString("status")!="valid" || !o->get("context") || !o->get("context")->getAsNull())
        return fail("UART requires checked base-context connections");
    } else return fail("unsupported UART frontend operation");
  }
  std::map<std::string,Obj*> named;
  int clocks=0;
  for (auto &v:items) {
    auto *o=v.getAsObject(); auto kind=o->getString("kind");
    if (kind=="test") continue;
    if (kind=="clock") { ++clocks; continue; }
    auto n=o->getString("name");
    if (!n || !named.emplace(n->str(),o).second) return fail("unnamed or duplicate UART declaration");
  }
  if (clocks!=1 || named.size()!=9 || streams.size()!=2) return fail("UART requires two streams and supported declarations");
  auto name=[](Obj *o)->std::string { auto n=o ? o->getString("name") : std::nullopt; return o && o->getString("kind")=="name" && n ? n->str() : ""; };
  auto isfield=[&](Obj *o,llvm::StringRef base,llvm::StringRef port) { return o && o->getString("kind")=="field" && o->getString("field")==port && name(o->getObject("object"))==base; };
  Obj *txstream=nullptr,*rxstream=nullptr;
  for (auto &v:items) {
    Obj *o=v.getAsObject(); if (o->getString("kind")!="stream") continue;
    auto from=name(o->getObject("from")),to=name(o->getObject("to"));
    auto *stages=o->getArray("stages"); auto packets=o->getBoolean("packets");
    if (o->getInteger("element_width_bits")!=8 || !stages || !stages->empty() || !packets || *packets)
      return fail("UART requires untransformed byte streams");
    if (from=="host" && !txstream) txstream=o;
    else if (to=="host" && !rxstream) rxstream=o;
    else return fail("UART requires separate host TX and RX streams");
  }
  if (!txstream || !rxstream) return fail("missing UART streams");
  std::string sender=name(txstream->getObject("to")), receiver=name(rxstream->getObject("from"));
  if (!named.count(sender) || !named.count(receiver) || sender==receiver) return fail("invalid UART stream bindings");
  auto fields=[](Obj *o,std::map<std::string,Obj*> &out) {
    if (!o || o->getString("kind")!="resource" || !o->getArray("fields")) return false;
    for (auto &v:*o->getArray("fields")) {
      auto *f=v.getAsObject(); auto n=f ? f->getString("name") : std::nullopt;
      if (!n || !f->get("value") || !out.emplace(n->str(),f->getObject("value")).second) return false;
    }
    return true;
  };
  auto frame=[&](Obj *f) {
    auto *a=f ? f->getArray("values") : nullptr;
    if (!f || f->getString("kind")!="list" || !a || a->size()!=3) return false;
    auto *start=(*a)[0].getAsObject(), *data=(*a)[1].getAsObject(), *stop=(*a)[2].getAsObject();
    auto *args=data ? data->getArray("args") : nullptr;
    auto *arg=args && args->size()==1 ? (*args)[0].getAsObject() : nullptr;
    auto *bits=arg ? arg->getObject("value") : nullptr;
    return start && start->getString("kind")=="integer" && start->getString("value")=="0" &&
      stop && stop->getString("kind")=="integer" && stop->getString("value")=="1" &&
      data && data->getString("kind")=="call" && name(data->getObject("callee"))=="data" &&
      arg && arg->get("name") && arg->get("name")->getAsNull() && bits && bits->getString("kind")=="integer" && bits->getString("value")=="8";
  };
  std::map<std::string,Obj*> txf,rxf;
  if (!fields(named[sender],txf) || !fields(named[receiver],rxf) || named[sender]->getString("resource_kind")!="shifter" ||
      named[receiver]->getString("resource_kind")!="shifter" || txf.size()!=3 || rxf.size()!=4 ||
      !frame(txf["frame"]) || !frame(rxf["frame"]) || !txf["idle"] || txf["idle"]->getString("kind")!="integer" || txf["idle"]->getString("value")!="1")
    return fail("unsupported UART shifter or frame");
  auto getbase=[&](Obj *o,llvm::StringRef port)->std::string {
    if (!o || o->getString("kind")!="field" || o->getString("field")!=port) return "";
    return name(o->getObject("object"));
  };
  auto txtimer=getbase(txf["shift"],"tick"),rxtimer=getbase(rxf["shift"],"tick"),rxpin=getbase(rxf["start"],"fall");
  auto *sample=rxf["sample"];
  if (txtimer.empty() || rxtimer.empty() || rxpin.empty() || !sample || sample->getString("kind")!="binary" ||
      sample->getString("op")!="at" || name(sample->getObject("left"))!=rxpin || !isfield(sample->getObject("right"),rxtimer,"tick"))
    return fail("unsupported UART sample or shift binding");
  if (!named.count(rxpin) || named[rxpin]->getString("kind")!="pin" || named[rxpin]->getString("mode")!="in" ||
      !named[rxpin]->get("pads") || !named[rxpin]->get("pads")->getAsNull() ||
      !named[rxpin]->get("source") || !named[rxpin]->get("source")->getAsNull()) return fail("unsupported UART RX pin");
  int txpins=0; std::string txpin;
  for (auto &kv:named) { Obj *o=kv.second;
    if (o->getString("kind")=="pin" && o->getString("mode")=="out" && isfield(o->getObject("source"),sender,"out") &&
        o->get("pads") && o->get("pads")->getAsNull()) {++txpins; txpin=kv.first;}
  }
  if (txpins!=1) return fail("unsupported UART TX pin");
  int64_t periods[2],firsts[2]; std::string constant;
  for (int i=0;i<2;++i) {
    auto timer=i ? rxtimer : txtimer;
    if (!named.count(timer) || named[timer]->getString("resource_kind")!="timer") return fail("missing UART timer");
    Obj *t=named[timer]; std::map<std::string,Obj*> f;
    if (!fields(t,f) || f.size()!=4 || !f.count("pulse") || f["pulse"] || !f.count("every") || !f.count("first") || !f.count("run")) return fail("unsupported UART timer fields");
    auto c=name(f["every"]); if (c.empty() || (!constant.empty() && constant!=c)) return fail("UART periods must share a constant"); constant=c;
    auto *run=f["run"];
    if (!run || run->getString("kind")!="binary" || run->getString("op")!="until" ||
        !(i ? isfield(run->getObject("left"),rxpin,"fall") : isfield(run->getObject("left"),sender,"busy")) ||
        !isfield(run->getObject("right"),i ? receiver : sender,"done")) return fail("unsupported UART timer run binding");
    auto *timings=t->getArray("timings"); bool gotPeriod=false,gotFirst=false;
    if (!timings) return fail("missing UART timings");
    for (auto &entry:*timings) {
      auto *pair=entry.getAsArray(); if (!pair || pair->size()!=2) return fail("invalid UART timing");
      auto key=(*pair)[0].getAsString(); auto *v=(*pair)[1].getAsObject();
      auto number=v ? v->getString("numerator") : std::nullopt; int64_t value=0;
      if (!key || !v || v->getString("status")!="known" || v->getString("denominator")!="1" || !number || number->getAsInteger(10,value)) return fail("UART timing must be integral");
      if (*key=="every" && !gotPeriod) {periods[i]=value;gotPeriod=true;}
      else if (*key=="first" && !gotFirst) {firsts[i]=value;gotFirst=true;}
      else return fail("unsupported UART timing");
    }
    if (!gotPeriod || !gotFirst || firsts[i]<1 || firsts[i]>periods[i] || periods[i]>1000000) return fail("invalid UART timing range");
  }
  if (periods[0]!=periods[1] || !named.count(constant) || named[constant]->getString("kind")!="constant") return fail("invalid UART period constant");
  auto txname=txstream->getString("name"), rxname=rxstream->getString("name");
  if (!txname || !rxname || std::set<std::string>{constant,txpin,rxpin,txtimer,rxtimer,sender,receiver,txname->str(),rxname->str()}.size()!=9)
    return fail("UART requires nine distinct source declarations");
  int64_t clock=0; if (root->getAttrOfType<StringAttr>("clock_hz").getValue().getAsInteger(10,clock) || clock<1 || clock>1000000000) return fail("invalid UART clock");
  auto &o=llvm::outs();
  o << "module {\n  \"spatial.workload_graph\"() { provenance = \"uart-8n1-source-component-v1\", analysis_only = true, clock_hz = " << clock << " : i64,\n    nodes = [\n";
  auto node=[&](const char *id,const char *resource,const char *opcode,const char *in,const char *out, std::string extra="") {
    o << "      {id = \"" << id << "\", resource = \"" << resource << "\", opcode = \"" << opcode << "\", inputs = [" << in << "], outputs = [" << out << "], duration = 1 : i64, exclusive = true" << extra << "},\n";
  };
  node("host_tx","host","stream_source","","i8"); node("host_rx","host","stream_sink","i8","");
  node("rx","input","input","","i1"); node("edge","edge","falling_edge","i1","i1");
  for (int i=0;i<2;++i) node(i?"rx_timer":"tx_timer","timer","uart_timer","i1, i1","i1",", period = "+std::to_string(periods[i])+" : i64, first = "+std::to_string(firsts[i])+" : i64");
  node("sender","shifter","uart_tx_8n1","i8, i1","i1, i1, i1"); node("receiver","shifter","uart_rx_8n1","i1, i1, i1","i8, i1");
  o << "      {id = \"tx\", resource = \"output\", opcode = \"output\", inputs = [i1], outputs = [], duration = 1 : i64, exclusive = true}\n    ], edges = [\n";
  struct Edge {const char *a; int ap; const char *b; int bp; const char *mode;};
  const Edge edges[]={{"host_tx",0,"sender",0,"data"},{"sender",0,"tx",0,"data"},{"sender",1,"tx_timer",0,"feedback"},{"sender",2,"tx_timer",1,"feedback"},{"tx_timer",0,"sender",1,"data"},{"rx",0,"edge",0,"data"},{"edge",0,"rx_timer",0,"data"},{"receiver",1,"rx_timer",1,"feedback"},{"rx_timer",0,"receiver",0,"data"},{"edge",0,"receiver",1,"data"},{"rx",0,"receiver",2,"data"},{"receiver",0,"host_rx",0,"data"}};
  for (size_t i=0;i<sizeof(edges)/sizeof(edges[0]);++i) {auto &e=edges[i]; o << "      {from = \"" << e.a << "\", source_port = " << e.ap << " : i64, to = \"" << e.b << "\", target_port = " << e.bp << " : i64, mode = \"" << e.mode << "\"}" << (i+1==sizeof(edges)/sizeof(edges[0]) ? "\n" : ",\n");}
  o << "    ]\n  } : () -> ()\n}\n"; return 0;
}
