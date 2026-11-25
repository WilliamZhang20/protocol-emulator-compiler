// Sequencer programs as lowerings produce them: rows with absolute successor
// indices, so a program may exceed the hardware's 16-row page. The image
// builder (grid_coded_image.py) splits longer programs into SRAM pages.
struct TemporalRow { int halt, no, yes, event, action; };

static TemporalRow temporalRow(int halt,int no,int yes,int event,int action) {
  return {halt,no,yes,event,action};
}

// `stretchable` lists rows whose entry may be delayed by a page load while
// the row's own action is held (for example an I2C phase that may hold SCL
// low longer). Page boundaries are only placed in front of such rows. A
// program that fits one page also gets the packed 16-row table, unchanged
// from earlier images.
static llvm::json::Object temporalProgram(const std::vector<TemporalRow> &rows,int entry,int idle,
                                          const std::vector<int> &stretchable) {
  llvm::json::Object t{{"entry",entry},{"idle_actions",idle}};
  llvm::json::Array flat;
  for (auto &r:rows) flat.push_back(llvm::json::Array{r.halt,r.no,r.yes,r.event,r.action});
  t["program"]=std::move(flat);
  llvm::json::Array s; for (int r:stretchable) s.push_back(r);
  t["stretchable_rows"]=std::move(s);
  if (rows.size()<=16) {
    llvm::json::Array packed;
    for (auto &r:rows) packed.push_back((r.halt<<15)|(r.no<<11)|(r.yes<<7)|(r.event<<4)|r.action);
    while (packed.size()<16) packed.push_back(1<<15);
    t["rows"]=std::move(packed);
  }
  return t;
}
