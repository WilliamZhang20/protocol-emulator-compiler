// Graph-model mapper: bounded placement + directed shortest paths + a simple
// non-preemptive resource schedule. It deliberately emits no hardware image.
static int mapWorkloadGraph(ModuleOp targetModule, ModuleOp workModule, bool assess=false) {
  Operation *target=findOne(targetModule,"spatial.target_graph");
  Operation *work=findOne(workModule,"spatial.workload_graph");
  if (!target || !work) return fail("expected one target_graph and one workload_graph");
  if (auto analysis=work->getAttrOfType<BoolAttr>("analysis_only"))
    if (analysis.getValue() && !assess) return fail("analysis-only graph has unimplemented operation semantics; hardware feasibility unknown");
  auto clock=work->getAttrOfType<IntegerAttr>("clock_hz");
  auto half=work->getAttrOfType<IntegerAttr>("half_period");
  auto pad=target->getAttrOfType<IntegerAttr>("pad_max_output_hz_estimate");
  bool padChecked=clock && half && pad;
  if (padChecked && clock.getInt()>2*half.getInt()*pad.getInt())
    return fail("nominal output frequency exceeds target's provisional pad estimate");
  struct Tile { std::string id,kind; int width,depth,latency,interval,states,controlInputs,periodMin,periodMax,firstMin,firstMax; std::set<std::string> operations; };
  struct Link { int from,to,width,latency; };
  struct Node { std::string id,kind,opcode,group; int width,duration,address,states,controlInputs,period,first; bool exclusive; };
  struct Edge { int from,to,width; std::string mode; };
  std::vector<Tile> tiles; std::vector<Link> links; std::vector<Node> nodes; std::vector<Edge> edges;
  std::map<std::string,int> tileId,nodeId;
  for (auto raw : target->getAttrOfType<ArrayAttr>("tiles")) {
    auto t=cast<DictionaryAttr>(raw);
    std::string id=t.getAs<StringAttr>("id").getValue().str();
    auto depth=t.getAs<IntegerAttr>("depth");
    tileId[id]=static_cast<int>(tiles.size());
    std::set<std::string> operations;
    for (auto op:t.getAs<ArrayAttr>("operations")) operations.insert(cast<StringAttr>(op).getValue().str());
    auto number=[&](const char *name) { auto value=t.getAs<IntegerAttr>(name); return value ? static_cast<int>(value.getInt()) : -1; };
    tiles.push_back({id,t.getAs<StringAttr>("resource").getValue().str(),
        static_cast<int>(t.getAs<IntegerAttr>("width").getInt()),depth ? static_cast<int>(depth.getInt()) : -1,
        static_cast<int>(t.getAs<IntegerAttr>("latency").getInt()),static_cast<int>(t.getAs<IntegerAttr>("interval").getInt()),
        number("states"),number("control_inputs"),number("period_min"),number("period_max"),number("first_min"),number("first_max"),std::move(operations)});
  }
  if (tiles.size()>64) return fail("graph search supports at most 64 tiles");
  for (auto raw : target->getAttrOfType<ArrayAttr>("links")) {
    auto l=cast<DictionaryAttr>(raw);
    links.push_back({tileId.at(l.getAs<StringAttr>("from").getValue().str()),
        tileId.at(l.getAs<StringAttr>("to").getValue().str()),
        static_cast<int>(l.getAs<IntegerAttr>("width").getInt()),
        static_cast<int>(l.getAs<IntegerAttr>("latency").getInt())});
  }
  for (auto raw : work->getAttrOfType<ArrayAttr>("nodes")) {
    auto n=cast<DictionaryAttr>(raw); std::string id=n.getAs<StringAttr>("id").getValue().str();
    int width=1;
    for (auto list : {n.getAs<ArrayAttr>("inputs"),n.getAs<ArrayAttr>("outputs")})
      for (auto t : list) width=std::max(width,static_cast<int>(cast<IntegerType>(cast<TypeAttr>(t).getValue()).getWidth()));
    auto group=n.getAs<StringAttr>("group"); auto address=n.getAs<IntegerAttr>("address");
    auto exclusive=n.getAs<BoolAttr>("exclusive");
    auto number=[&](const char *name) { auto value=n.getAs<IntegerAttr>(name); return value ? static_cast<int>(value.getInt()) : -1; };
    if (address && address.getInt()>1000000) return fail("graph memory address exceeds supported range");
    nodeId[id]=static_cast<int>(nodes.size());
    nodes.push_back({id,n.getAs<StringAttr>("resource").getValue().str(),n.getAs<StringAttr>("opcode").getValue().str(),group ? group.getValue().str() : "",
        width,static_cast<int>(n.getAs<IntegerAttr>("duration").getInt()),address ? static_cast<int>(address.getInt()) : -1,
        number("states"),number("control_inputs"),number("period"),number("first"),
        exclusive && exclusive.getValue()});
  }
  if (nodes.size()>64 || links.size()>512) return fail("graph search size exceeds supported range");
  for (auto raw : work->getAttrOfType<ArrayAttr>("edges")) {
    auto e=cast<DictionaryAttr>(raw); int from=nodeId.at(e.getAs<StringAttr>("from").getValue().str());
    int to=nodeId.at(e.getAs<StringAttr>("to").getValue().str());
    auto n=cast<DictionaryAttr>(work->getAttrOfType<ArrayAttr>("nodes")[from]);
    int port=static_cast<int>(e.getAs<IntegerAttr>("source_port").getInt());
    int width=static_cast<int>(cast<IntegerType>(cast<TypeAttr>(n.getAs<ArrayAttr>("outputs")[port]).getValue()).getWidth());
    edges.push_back({from,to,width,e.getAs<StringAttr>("mode").getValue().str()});
  }
  if (edges.size()>1024) return fail("graph search supports at most 1024 edges");
  auto compatibleTile=[](const Node &node,const Tile &tile) {
    return tile.kind==node.kind && tile.operations.count(node.opcode) && tile.width>=node.width &&
      (node.address<0 || (tile.depth>=0 && node.address<tile.depth)) &&
      (node.opcode!="protocol" || (node.states<=tile.states && node.controlInputs<=tile.controlInputs)) &&
      (node.opcode!="timer" || (node.period>=tile.periodMin && node.period<=tile.periodMax)) &&
      (node.opcode!="uart_timer" || (node.period>=tile.periodMin && node.period<=tile.periodMax && node.first>=tile.firstMin && node.first<=tile.firstMax));
  };
  llvm::json::Array deficits;
  if (assess) {
    std::map<std::string,int> needed,available;
    for (const auto &n:nodes) {
      if (n.exclusive) ++needed[n.kind];
      bool found=false,kindExists=false,opExists=false,widthExists=false;
      for (const auto &t:tiles) {
        found |= compatibleTile(n,t);
        if (t.kind==n.kind) {kindExists=true;if (t.operations.count(n.opcode)) {opExists=true;widthExists |= t.width>=n.width;}}
      }
      if (!found) {
        const char *reason=!kindExists ? "resource_absent" : !opExists ? "operation_unsupported" : !widthExists ? "width_exceeded" : "capability_limits";
        llvm::json::Object issue{{"kind","no_compatible_tile"},{"reason",reason},{"node",n.id},{"resource",n.kind},{"operation",n.opcode},{"width",n.width}};
        if (n.opcode=="uart_timer") {issue["period"]=n.period;issue["first"]=n.first;}
        deficits.push_back(std::move(issue));
      }
    }
    for (const auto &t:tiles) ++available[t.kind];
    for (const auto &[kind,count]:needed) if (count>available[kind])
      deficits.push_back(llvm::json::Object{{"kind","exclusive_capacity"},{"resource",kind},{"required",count},{"available",available[kind]}});
    if (!deficits.empty()) {
      llvm::json::Object result{{"model","typed-resource-assessment-v1"},{"status","infeasible_in_model"},
        {"deficits",std::move(deficits)},{"rtl_fidelity_verified",false},{"timing_feasibility_verified",false},
        {"limitations","Checks only declared resource capabilities and exclusive counts; source graph and target are abstractions. Missing host boundary capabilities may reflect incomplete modeling, not missing RTL."}};
      llvm::outs()<<llvm::formatv("{0:2}\n",llvm::json::Value(std::move(result))); return 0;
    }
  }
  // Feedback links must route, but don't impose acyclic one-shot ordering.
  std::vector<int> degree(nodes.size(),0), order;
  for (auto &e:edges) if (e.mode!="feedback") ++degree[e.to];
  std::deque<int> pending;
  for (size_t i=0;i<nodes.size();++i) if (!degree[i]) pending.push_back(static_cast<int>(i));
  while (!pending.empty()) {
    int u=pending.front(); pending.pop_front(); order.push_back(u);
    for (auto &e:edges) if (e.mode!="feedback" && e.from==u && --degree[e.to]==0) pending.push_back(e.to);
  }
  if (order.size()!=nodes.size()) return fail("workload has a non-feedback dependency cycle");
  // Shortest directed path, with only explicit route tiles allowed as relays.
  auto path=[&](int from,int to,int bits,std::vector<int> *out)->int {
    if (from==to) { if (out) *out={from}; return 0; }
    const int INF=100000000;
    std::vector<int> dist(tiles.size(),INF),prev(tiles.size(),-1);
    using Q=std::pair<int,int>; std::priority_queue<Q,std::vector<Q>,std::greater<Q>> q;
    dist[from]=0; q.emplace(0,from);
    while (!q.empty()) {
      auto [d,u]=q.top();q.pop();if(d!=dist[u])continue;
      if (u==to) break;
      if (u!=from && tiles[u].kind!="route") continue;
      for (auto &l:links) if (l.from==u && l.width>=bits && d+l.latency<dist[l.to]) {
        dist[l.to]=d+l.latency;prev[l.to]=u;q.emplace(dist[l.to],l.to);
      }
    }
    if (dist[to]==INF) return -1;
    if (out) { out->clear();for(int p=to;p!=-1;p=prev[p])out->push_back(p);std::reverse(out->begin(),out->end()); }
    return dist[to];
  };
  std::vector<int> placed(nodes.size(),-1), usage(tiles.size(),0);
  std::vector<bool> exclusiveTile(tiles.size(),false);
  std::map<std::string,int> groups;
  int64_t search=0; bool exhausted=false;
  std::function<bool(size_t)> place=[&](size_t index) {
    if (++search>100000) { exhausted=true; return false; }
    if (index==nodes.size()) return true;
    int n=order[index]; auto &node=nodes[n];
    for (size_t i=0;i<tiles.size();++i) {
      auto &tile=tiles[i];
      if (!compatibleTile(node,tile) || exclusiveTile[i] || (node.exclusive && usage[i])) continue;
      auto existing=groups.find(node.group);
      if (!node.group.empty() && existing!=groups.end() && existing->second!=static_cast<int>(i)) continue;
      placed[n]=static_cast<int>(i); bool compatible=true;
      for (auto &e:edges) if (e.mode!="order" && placed[e.from]>=0 && placed[e.to]>=0 &&
          path(placed[e.from],placed[e.to],e.width,nullptr)<0) {compatible=false;break;}
      bool inserted=false;
      if (compatible && !node.group.empty() && existing==groups.end()) {groups[node.group]=static_cast<int>(i);inserted=true;}
      if (compatible) {
        ++usage[i]; if (node.exclusive) exclusiveTile[i]=true;
        if (place(index+1)) return true;
        --usage[i]; if (node.exclusive) exclusiveTile[i]=false;
      }
      if (inserted) groups.erase(node.group);
      placed[n]=-1;
      if (exhausted) return false;
    }
    return false;
  };
  if (!place(0)) {
    if (!assess) return fail(exhausted ? "graph search limit reached; feasibility unknown" : "no compatible graph placement and routes");
    llvm::json::Object result{{"model","typed-resource-assessment-v1"},{"status",exhausted ? "unknown_search_limit" : "infeasible_in_model"},
      {"reason",exhausted ? "bounded search exhausted" : "no placement satisfies the declared directed paths and exclusive resources"},
      {"rtl_fidelity_verified",false},{"timing_feasibility_verified",false}};
    llvm::outs()<<llvm::formatv("{0:2}\n",llvm::json::Value(std::move(result))); return 0;
  }
  std::vector<int64_t> start(nodes.size(),0),finish(nodes.size(),0),available(tiles.size(),0);
  int64_t contention=0;
  for (int n:order) {
    int64_t earliest=0;
    for (auto &e:edges) if (e.to==n && e.mode!="feedback") {
      int latency=e.mode=="order" ? 0 : path(placed[e.from],placed[e.to],e.width,nullptr);
      earliest=std::max(earliest,finish[e.from]+latency);
    }
    start[n]=std::max(earliest,available[placed[n]]);
    contention+=start[n]-earliest;
    finish[n]=start[n]+std::max(nodes[n].duration,tiles[placed[n]].latency);
    available[placed[n]]=start[n]+tiles[placed[n]].interval;
  }
  llvm::json::Array assignments,schedule,routes,resources;
  for (size_t i=0;i<tiles.size();++i) {
    llvm::json::Array assigned;
    std::set<int> addresses;
    for (size_t n=0;n<nodes.size();++n) if (placed[n]==static_cast<int>(i)) assigned.push_back(nodes[n].id);
    llvm::json::Object resource{{"tile",tiles[i].id},{"kind",tiles[i].kind},
        {"width",tiles[i].width},{"assigned_nodes",std::move(assigned)}};
    if (tiles[i].depth>=0) {
      for (size_t n=0;n<nodes.size();++n)
        if (placed[n]==static_cast<int>(i) && nodes[n].address>=0) addresses.insert(nodes[n].address);
      llvm::json::Array touched; for (int address:addresses) touched.push_back(address);
      resource["capacity_words"]=tiles[i].depth;
      resource["touched_addresses"]=std::move(touched);
      resource["address_allocation_verified"]=false;
    }
    resources.push_back(std::move(resource));
  }
  for (size_t n=0;n<nodes.size();++n) {
    assignments.push_back(llvm::json::Object{{"node",nodes[n].id},{"tile",tiles[placed[n]].id}});
    llvm::json::Object entry{{"node",nodes[n].id},{"tile",tiles[placed[n]].id},{"start",start[n]},{"finish",finish[n]}};
    if (nodes[n].address>=0) entry["address"]=nodes[n].address;
    schedule.push_back(std::move(entry));
  }
  for (auto &e:edges) if (e.mode!="order") {
    std::vector<int> p; int latency=path(placed[e.from],placed[e.to],e.width,&p);
    llvm::json::Array names; for(int t:p) names.push_back(tiles[t].id);
    routes.push_back(llvm::json::Object{{"from",nodes[e.from].id},{"to",nodes[e.to].id},
        {"width",e.width},{"mode",e.mode},{"latency",latency},{"tiles",std::move(names)}});
  }
  int64_t end=0;for (auto t:finish) end=std::max(end,t);
  llvm::json::Object report{{"model","typed-resource-graph-v1"},{"assignments",std::move(assignments)},
      {"schedule",std::move(schedule)},{"routes",std::move(routes)}, {"resources",std::move(resources)}, {"search_steps",search},
      {"resource_wait_cycles",contention},{"model_makespan",end},{"image_emitted",false},
      {"rtl_fidelity_verified",false},{"physical_timing_verified",false},{"full_source_program_compiled",false},
      {"nominal_pad_frequency_checked",padChecked},
      {"limitations","First feasible bounded placement; resource schedule uses declared abstract durations, not RTL-cycle-accurate timing. Feedback routes are checked but not scheduled; no route-selector conflicts, link traffic capacity, value-lifetime/storage allocation, host transaction model, physical timing, or general .spt stream lowering."}};
  if (auto name=target->getAttrOfType<StringAttr>("name")) report["target"]=name.getValue();
  if (assess) {
    report["model"]="typed-resource-assessment-v1";
    report["status"]="abstract_placement_found";
    report["timing_feasibility_verified"]=false;
    report.erase("schedule"); report.erase("model_makespan"); report.erase("resource_wait_cycles");
  }
  if (auto source=work->getAttrOfType<StringAttr>("provenance")) report["workload_provenance"]=source.getValue();
  if (auto origin=work->getAttrOfType<StringAttr>("memory_layout_origin")) report["memory_layout_origin"]=origin.getValue();
  llvm::outs() << llvm::formatv("{0:2}\n",llvm::json::Value(std::move(report)));
  return 0;
}
