#!/bin/sh
set -eu
: "${CIRCT_PREFIX:?Set CIRCT_PREFIX to a CIRCT install containing include/ and lib/}"
CXX=${CXX:-c++}
OUT=${OUT:-/tmp/spatial-circt-map}
HERE=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
PLUGIN_DIR=$(dirname -- "$OUT")
"$CXX" -std=c++17 -O1 -fno-rtti -fPIC -shared -I"$CIRCT_PREFIX/include" "$HERE/dialect.cpp" \
  -L"$CIRCT_PREFIX/lib" -Wl,--allow-shlib-undefined -Wl,-rpath,"$CIRCT_PREFIX/lib" \
  -Wl,-soname,libSpatialDialect.so -lMLIRIR -lMLIRSupport -lLLVMSupport \
  -o "$PLUGIN_DIR/libSpatialDialect.so"
"$CXX" -std=c++17 -O1 -fno-rtti -I"$CIRCT_PREFIX/include" "$HERE/map.cpp" \
  -L"$CIRCT_PREFIX/lib" -L"$PLUGIN_DIR" -Wl,--allow-shlib-undefined -Wl,-rpath,"$CIRCT_PREFIX/lib" -Wl,-rpath,"$PLUGIN_DIR" \
  -lSpatialDialect \
  -lCIRCTFSM -lCIRCTHW -lCIRCTComb -lMLIRParser -lMLIRIR -lMLIRSupport -lLLVMCore -lLLVMSupport \
  -o "$OUT"
printf 'Built %s\n' "$OUT"
