// Restricted, source-validated image generation for the experimental v2
// fabric. Placement and SRAM stream framing are currently fixed; this is not
// general graph mapping or full .spt transaction lowering.
static int compileV2Image(ModuleOp frontend, const char *output, bool uart, bool streaming=false, bool unified=false) {
  std::string text;
  llvm::raw_string_ostream stream(text);
  int rc=uart ? lowerUart(frontend,stream) : compileMemorySpi(frontend,"0",nullptr,true,stream);
  if (rc) return rc;
  stream.flush();
  auto graph=parseSourceString<ModuleOp>(text,frontend.getContext());
  if (!graph || failed(verify(*graph))) return fail("generated v2 source graph failed verification");
  Operation *work=findOne(*graph,"spatial.workload_graph");
  if (!work) return fail("missing v2 source graph");
  std::array<uint16_t,27> words{};
  auto route=[&](int sink,int source) {words[6+sink]=static_cast<uint16_t>(source);};
  // Fixed named port assignment for this experiment. Each sink owns its own
  // selector, so no selector is shared by two independently driven sinks.
  route(0,2);route(1,15);route(6,2);
  if (!uart) {
    auto rows=work->getAttrOfType<ArrayAttr>("control_rows");
    const int expected[]={0,0,9,9,1,16,1,16,2,2,2,2,3,3,3,3};
    if (!rows || rows.size()!=16) return fail("v2 SPI control table unavailable");
    for (size_t i=0;i<16;++i)
      if (cast<IntegerAttr>(rows[i]).getInt()!=expected[i])
        return fail("v2 SPI requires the supported trigger/busy/done control behavior");
    int64_t half=0; if (!integerAttr(work,"half_period",half) || half<1 || half>65535) return fail("invalid v2 SPI period");
    int width=0;
    for (auto raw:work->getAttrOfType<ArrayAttr>("nodes")) {
      auto n=cast<DictionaryAttr>(raw);
      if (n.getAs<StringAttr>("id").getValue()=="shift")
        width=cast<IntegerType>(cast<TypeAttr>(n.getAs<ArrayAttr>("inputs")[0]).getValue()).getWidth();
    }
    if (width<9 || width>16) return fail("v2 SPI SRAM adapter supports 9..16 bits");
    words[0]=words[1]=static_cast<uint16_t>(half);words[2]=words[3]=1;
    words[4]=static_cast<uint16_t>(32+width);words[5]=1;
    route(7,8);route(8,7);route(9,4);route(14,16);route(15,9);
    words[22]=0x000a;words[23]=0;words[24]=24;words[25]=7;
  } else {
    int64_t period[2]={0,0},first[2]={0,0};
    for (auto raw:work->getAttrOfType<ArrayAttr>("nodes")) {
      auto n=cast<DictionaryAttr>(raw);auto id=n.getAs<StringAttr>("id").getValue();
      int i=id=="tx_timer" ? 0 : id=="rx_timer" ? 1 : -1;
      if(i>=0) {period[i]=n.getAs<IntegerAttr>("period").getInt();first[i]=n.getAs<IntegerAttr>("first").getInt();}
    }
    if(first[0]<1 || first[1]<1 || period[0]<1 || period[1]<1 || first[0]>65535 || first[1]>65535 || period[0]>65535 || period[1]>65535)
      return fail("v2 UART timer value exceeds 16-bit range");
    if (period[0]<16 || period[1]<16)
      return fail("v2 UART periods below 16 cycles are not supported by this experiment");
    words[0]=static_cast<uint16_t>(first[0]);words[1]=static_cast<uint16_t>(period[0]);
    words[2]=static_cast<uint16_t>(first[1]);words[3]=static_cast<uint16_t>(period[1]);
    words[4]=words[5]=10;
    route(3,23);route(4,19);route(7,6);route(10,23);route(11,10);route(12,10);route(13,5);route(14,17);
    words[22]=0x0235;words[23]=0x0200;words[24]=0;words[25]=1;
  }
  std::ofstream image(output);
  if (!image) return fail("cannot open v2 image");
  words[26]=uart ? 1 : 0;
  for (size_t i=0;i<(unified ? 27u : 26u);++i) image << std::hex << std::setfill('0') << std::setw(4) << words[i] << '\n';
  if (!image) return fail("cannot write v2 image");
  llvm::json::Object report{{"target",unified ? "fabric-v2-unified-experiment" : streaming ? "fabric-v2-uart-stream-experiment" : "fabric-v2-memory-experiment"}, {"component",uart ? "uart-8n1" : "memory-spi"},
      {"source_validated",true},{"general_mapping",false},{"full_source_program_compiled",false},
      {"limitations",(streaming || unified) ? "Fixed placement, routes and hand-written SRAM adapters; host supplies memory regions and lengths; no general stream/function lowering, framing-error detection, static route conflict analysis, or pin timing proof." :
       "Fixed placement and route assignment; finite hand-written SRAM adapter; no general stream/function lowering, static route conflict analysis, pin timing proof, or RTL equivalence guarantee."}};
  llvm::outs()<<llvm::formatv("{0:2}\n",llvm::json::Value(std::move(report)));return 0;
}
