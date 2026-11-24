// Fixed tile placement with a real finite-track router for the 3x3 RTL grid.
// This is a restricted source-derived SPI component, not a general mapper.
static int compileGridImage(ModuleOp frontend,const char *output,bool uart) {
  std::string text; llvm::raw_string_ostream stream(text);
  int rc=uart ? lowerUart(frontend,stream) : compileMemorySpi(frontend,"0",nullptr,true,stream);
  if (rc) return rc;
  stream.flush(); auto graph=parseSourceString<ModuleOp>(text,frontend.getContext());
  if (!graph || failed(verify(*graph))) return fail("grid source graph failed verification");
  Operation *work=findOne(*graph,"spatial.workload_graph");
  if (!work) return fail("missing grid workload");
  struct Destination {int cell,sink;};
  struct Net {const char *name;int source,port;std::vector<Destination> destinations;};
  std::vector<Net> nets;
  struct State {std::array<uint16_t,66> image{};int owner[9][4][4]{};int lane[12]{};};
  State initial;
  if (!uart) {
    auto rows=work->getAttrOfType<ArrayAttr>("control_rows");
    const int expected[]={0,0,9,9,1,16,1,16,2,2,2,2,3,3,3,3};
    if (!rows || rows.size()!=16) return fail("grid SPI control table unavailable");
    for (size_t i=0;i<16;++i) if (cast<IntegerAttr>(rows[i]).getInt()!=expected[i])
      return fail("grid SPI requires supported trigger/busy/done behavior");
    int64_t half=0; if (!integerAttr(work,"half_period",half) || half<1 || half>65535) return fail("invalid grid SPI period");
    int width=0;
    for (auto raw:work->getAttrOfType<ArrayAttr>("nodes")) {
      auto n=cast<DictionaryAttr>(raw);
      if (n.getAs<StringAttr>("id").getValue()=="shift")
        width=cast<IntegerType>(cast<TypeAttr>(n.getAs<ArrayAttr>("inputs")[0]).getValue()).getWidth();
    }
    if (width<9 || width>16) return fail("grid SPI SRAM adapter supports 9..16 bits");
    nets={{"start",6,0,{{2,0},{3,3}}}, {"shift_done",3,1,{{2,1}}},
      {"timer_fall",2,2,{{3,4}}}, {"timer_rise",2,1,{{3,5}}},
      {"miso",0,0,{{3,6}}}, {"mosi",3,2,{{1,7}}},
      {"sck",2,3,{{1,8}}}, {"chip_select",4,0,{{1,9}}}, {"busy",3,0,{{4,10}}}};
    initial.image[0]=initial.image[1]=static_cast<uint16_t>(half);
    initial.image[2]=static_cast<uint16_t>(32+width);
    initial.image[3]=5;initial.image[4]=7;
    initial.image[53]=initial.image[54]=initial.image[55]=1;
    initial.image[63]=0x000a;
  } else {
    int64_t period[2]={0,0},first[2]={0,0};
    for(auto raw:work->getAttrOfType<ArrayAttr>("nodes")) {
      auto n=cast<DictionaryAttr>(raw);auto id=n.getAs<StringAttr>("id").getValue();
      int i=id=="tx_timer" ? 0 : id=="rx_timer" ? 1 : -1;
      if(i>=0) {period[i]=n.getAs<IntegerAttr>("period").getInt();first[i]=n.getAs<IntegerAttr>("first").getInt();}
    }
    if(first[0]<1 || first[1]<1 || period[0]<16 || period[1]<16 || first[0]>65535 || first[1]>65535 || period[0]>65535 || period[1]>65535)
      return fail("unsupported grid UART timer values");
    nets={{"tx_start",6,0,{{2,0},{3,3}}}, {"tx_done",3,1,{{2,1}}},
      {"tx_tick",2,0,{{3,4}}}, {"tx_wire",3,3,{{1,7}}},
      {"rx_fall",0,3,{{5,12},{7,15}}}, {"rx_done",7,1,{{5,13}}},
      {"rx_tick",5,0,{{7,16},{7,17}}}, {"rx_wire",0,1,{{7,18}}}};
    initial.image[0]=first[0];initial.image[1]=period[0];initial.image[2]=10;
    initial.image[53]=first[1];initial.image[54]=period[1];initial.image[55]=10;
    initial.image[4]=1;initial.image[63]=0x235;initial.image[64]=0x200;initial.image[65]=1;
  }
  auto direction=[](int cell,int destination,int lane) {
    if (lane>=2 && cell/3!=destination/3) return cell/3<destination/3 ? 1 : 0;
    if (cell%3!=destination%3) return cell%3<destination%3 ? 3 : 2;
    return cell/3<destination/3 ? 1 : 0;
  };
  std::function<bool(size_t,State&)> route=[&](size_t index,State &state) {
    if(index==nets.size()) return true;
    const Net &net=nets[index];
    for(int lane=0;lane<4;++lane) {
      State trial=state; bool ok=true;
      for(const auto &dst:net.destinations) {
        int cell=net.source, prev=-1;
        while(cell!=dst.cell) {
          int dir=direction(cell,dst.cell,lane);
          bool turn=prev>=0 && ((dir<2)!=(prev<2));
          int selection=prev<0 ? 1+net.port : turn ? ((prev==3 || prev==1) ? 6 : 7) : 5;
          int &owner=trial.owner[cell][dir][lane];
          if(owner && owner!=int(index)+1) {ok=false;break;}
          unsigned addr=5+cell*4+dir, shift=lane*3;
          int old=(trial.image[addr]>>shift)&7;
          if(owner && old!=selection) {ok=false;break;}
          owner=int(index)+1;
          trial.image[addr]=(trial.image[addr]&~(7u<<shift)) | (selection<<shift);
          cell += dir==0 ? -3 : dir==1 ? 3 : dir==2 ? -1 : 1;
          prev=dir;
        }
        if(!ok) break;
        if(prev<0) trial.image[dst.sink<12 ? 41+dst.sink : 56+dst.sink-12]=2+net.port;
        else trial.image[dst.sink<12 ? 41+dst.sink : 56+dst.sink-12]=(prev==0 ? 10 : prev==1 ? 6 : prev==2 ? 18 : 14)+lane;
      }
      if(ok) {trial.lane[index]=lane;if(route(index+1,trial)){state=trial;return true;}}
    }
    return false;
  };
  if(!route(0,initial)) return fail("grid routing failed: no available track for signal paths");
  std::ofstream image(output); if(!image) return fail("cannot open grid image");
  for(auto word:initial.image) image<<std::hex<<std::setfill('0')<<std::setw(4)<<word<<'\n';
  if(!image) return fail("cannot write grid image");
  llvm::json::Array routes;
  for(size_t i=0;i<nets.size();++i) {
    llvm::json::Array paths;
    for (const auto &dst:nets[i].destinations) {
      llvm::json::Array hops;
      int cell=nets[i].source;
      while(cell!=dst.cell) {
        int dir=direction(cell,dst.cell,initial.lane[i]);
        int next=cell+(dir==0 ? -3 : dir==1 ? 3 : dir==2 ? -1 : 1);
        int selection=(initial.image[5+cell*4+dir]>>(3*initial.lane[i]))&7;
        hops.push_back(llvm::json::Object{{"from",cell},{"to",next},{"direction",dir},{"selector",selection}});
        cell=next;
      }
      paths.push_back(llvm::json::Object{{"sink",dst.sink},{"cell",dst.cell},{"hops",std::move(hops)}});
    }
    routes.push_back(llvm::json::Object{{"signal",nets[i].name},{"source_cell",nets[i].source},
      {"source_port",nets[i].port},{"track",initial.lane[i]},{"paths",std::move(paths)}});
  }
  llvm::json::Object report{{"target",uart ? "grid-3x3-uart-experiment" : "grid-3x3-spi-experiment"},{"source_validated",true},
    {"general_mapping",false},{"full_source_program_compiled",false},{"routing",std::move(routes)},
    {"host_configuration",llvm::json::Object{{"tx_length_word",66},{"rx_capacity_word",67},{"pad_feedback_word",68},{"pad_feedback_value",0}}},
    {"limitations","Fixed tile placement and hand-written SRAM adapters; two turn-restricted route classes, no general stream/function lowering or physical timing proof."}};
  llvm::outs()<<llvm::formatv("{0:2}\n",llvm::json::Value(std::move(report)));return 0;
}
