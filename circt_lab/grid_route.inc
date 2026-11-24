// Exact switch-track routing for the registered spatial.grid_route_problem.
// Paths retain one track and follow that track's RTL turn class. Fanout can
// share a driven segment or use different tracks. This does not place tiles.
static int routeGridProblem(ModuleOp module) {
  Operation *op=findOne(module,"spatial.grid_route_problem");
  if (!op) return fail("expected one grid_route_problem");
  int cols=op->getAttrOfType<IntegerAttr>("columns").getInt();
  auto sw=op->getAttrOfType<ArrayAttr>("switch_selectors"), ins=op->getAttrOfType<ArrayAttr>("input_selectors");
  auto val=[](ArrayAttr a,int i){return static_cast<int>(cast<IntegerAttr>(a[i]).getInt());};
  bool allRegistered=op->getAttrOfType<BoolAttr>("all_registered").getValue();
  std::set<std::pair<int,int>> registered;
  llvm::json::Array registeredJson;
  for(auto raw:op->getAttrOfType<ArrayAttr>("registered_links")) {
    auto d=cast<DictionaryAttr>(raw);int a=d.getAs<IntegerAttr>("from").getInt(),b=d.getAs<IntegerAttr>("to").getInt();
    registered.emplace(a,b);registeredJson.push_back(llvm::json::Array{a,b});
  }
  auto trackAttr=op->getAttrOfType<ArrayAttr>("tracks");
  std::vector<std::string> classes;
  for (auto t:trackAttr) classes.push_back(cast<StringAttr>(t).getValue().str());
  std::map<std::string,int> loc;
  llvm::json::Object layout;
  for (auto raw:op->getAttrOfType<ArrayAttr>("layout")) {
    auto d=cast<DictionaryAttr>(raw); std::string name=d.getAs<StringAttr>("resource").getValue().str();
    int cell=d.getAs<IntegerAttr>("cell").getInt(); loc[name]=cell; layout[name]=cell;
  }
  struct Endpoint {std::string resource; int port;};
  struct Hop {int from,to,dir,select;};
  struct Path {int track,sinkSelect; std::vector<Hop> hops;};
  struct Edge {std::string name; Endpoint source,sink; std::vector<Path> options;};
  auto endpoint=[](DictionaryAttr d) -> Endpoint {return {d.getAs<StringAttr>("resource").getValue().str(),static_cast<int>(d.getAs<IntegerAttr>("port").getInt())};};
  std::vector<Edge> edges;
  for (auto raw:op->getAttrOfType<ArrayAttr>("nets")) {
    auto n=cast<DictionaryAttr>(raw); std::string name=n.getAs<StringAttr>("name").getValue().str();
    auto source=endpoint(n.getAs<DictionaryAttr>("source"));
    for (auto rawSink:n.getAs<ArrayAttr>("sinks")) {
      auto sink=endpoint(cast<DictionaryAttr>(rawSink)); Edge edge{name,source,sink,{}};
      for (size_t track=0;track<classes.size();++track) {
        int src=loc.at(source.resource),dest=loc.at(sink.resource);
        if (src==dest && sink.resource=="lut") continue;
        Path path{static_cast<int>(track),val(ins,0)+source.port,{}};
        int row=src/cols,col=src%cols,tr=dest/cols,tc=dest%cols,prev=-1;
        bool horizFirst=classes[track]=="horizontal_then_vertical";
        for (int axis=0;axis<2;++axis) {
          bool horiz=(axis==0)==horizFirst;
          while (horiz ? col!=tc : row!=tr) {
            int direction=horiz ? (col<tc ? 3 : 2) : (row<tr ? 1 : 0);
            int from=row*cols+col;
            if(direction==0)--row; else if(direction==1)++row; else if(direction==2)--col; else ++col;
            bool turn=prev>=0 && ((direction<2)!=(prev<2));
            int select=prev<0 ? val(sw,1)+source.port : turn ? ((prev==1 || prev==3) ? val(sw,3) : val(sw,4)) : val(sw,2);
            path.hops.push_back({from,row*cols+col,direction,select});prev=direction;
          }
        }
        if(prev>=0) path.sinkSelect=(prev==0 ? val(ins,2) : prev==1 ? val(ins,1) : prev==2 ? val(ins,4) : val(ins,3))+track;
        edge.options.push_back(std::move(path));
      }
      edges.push_back(std::move(edge));
    }
  }
  if (edges.size()>1024) return fail("too many routing endpoints");
  std::stable_sort(edges.begin(),edges.end(),[](const Edge &a,const Edge &b) {
    if(a.options.size()!=b.options.size()) return a.options.size()<b.options.size();
    if((a.name=="mosi")!=(b.name=="mosi")) return a.name=="mosi";
    size_t ah=a.options.empty()?0:a.options[0].hops.size(),bh=b.options.empty()?0:b.options[0].hops.size();
    if(ah!=bh) return ah>bh;
    if(a.name!=b.name) return a.name<b.name;
    if(a.sink.resource!=b.sink.resource) return a.sink.resource<b.sink.resource;
    return a.sink.port<b.sink.port;
  });
  using Key=std::tuple<int,int,int>;
  std::map<Key,std::pair<std::string,int>> owners;
  std::vector<int> selected(edges.size(),-1);
  int64_t visits=0;bool exhausted=false;
  int64_t searchLimit=op->getAttrOfType<IntegerAttr>("search_limit").getInt();
  std::function<bool(size_t)> solve=[&](size_t index) {
    if(++visits>searchLimit){exhausted=true;return false;}
    if(index==edges.size())return true;
    auto &edge=edges[index];
    for(size_t option=0;option<edge.options.size();++option){
      auto &path=edge.options[option]; bool valid=true;
      for(auto &h:path.hops){auto it=owners.find({h.from,h.dir,path.track});
        if(it!=owners.end() && it->second!=std::make_pair(edge.name,h.select)){valid=false;break;}}
      if(!valid)continue;
      std::vector<Key> added;
      for(auto &h:path.hops){Key key{h.from,h.dir,path.track};if(!owners.count(key)){owners[key]={edge.name,h.select};added.push_back(key);}}
      selected[index]=option;
      if(solve(index+1))return true;
      for(auto &key:added)owners.erase(key);
      selected[index]=-1;if(exhausted)return false;
    }
    return false;
  };
  bool found=solve(0);int maxHops=0,maxCycles=0;
  llvm::json::Array output;
  if(found) for(size_t i=0;i<edges.size();++i){
    auto &edge=edges[i];auto &path=edge.options[selected[i]];llvm::json::Array hops;
    maxHops=std::max(maxHops,static_cast<int>(path.hops.size()));
    int cycles=0;
    for(auto &h:path.hops){hops.push_back(llvm::json::Object{{"from",h.from},{"to",h.to},{"direction",h.dir},{"selector",h.select}});if(allRegistered || registered.count({h.from,h.to}))++cycles;}
    maxCycles=std::max(maxCycles,cycles);
    output.push_back(llvm::json::Object{{"net",edge.name},{"source",llvm::json::Object{{"resource",edge.source.resource},{"port",edge.source.port}}},
      {"sink",llvm::json::Object{{"resource",edge.sink.resource},{"port",edge.sink.port}}},
      {"track",path.track},{"hops",std::move(hops)},{"sink_select",path.sinkSelect},{"route_cycles",cycles}});
  }
  llvm::json::Object report{{"schema","spatial.grid-route-experiment.v1"},
    {"workload",op->getAttrOfType<StringAttr>("name").getValue()},
    {"status",found?"routed":exhausted?"unknown_search_limit":"unroutable_in_model"},
    {"track_count",static_cast<int>(classes.size())},{"layout",std::move(layout)},
    {"registered_hops",allRegistered?llvm::json::Value(true):registered.empty()?llvm::json::Value(false):llvm::json::Value(std::move(registeredJson))},{"direct_links",llvm::json::Array{}},{"search_nodes",visits},
    {"route_count",found?static_cast<int>(edges.size()):0},{"routes",std::move(output)},
    {"used_directed_track_segments",found?llvm::json::Value(static_cast<int64_t>(owners.size())):llvm::json::Value(nullptr)},
    {"max_hops",found?llvm::json::Value(maxHops):llvm::json::Value(nullptr)},
    {"max_route_cycles",found?llvm::json::Value(maxCycles):llvm::json::Value(nullptr)},
    {"engine","circt-spatial-grid-route-v1"},
    {"full_source_program_compiled",false},{"physical_timing_verified",false},
    {"limitations","Fixed placements and explicit bit nets; no word streams, temporal control, pad timing or full source compilation."}};
  llvm::outs()<<llvm::formatv("{0:2}\n",llvm::json::Value(std::move(report)));
  return 0;
}
