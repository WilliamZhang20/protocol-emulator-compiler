#include "Proto/ProtoDialect.h"

#include "mlir/IR/DialectRegistry.h"
#include "mlir/Tools/mlir-opt/MlirOptMain.h"

int main(int argc, char **argv) {
   mlir::DialectRegistry registry;
   
   registry.insert<proto::ProtoDialect>();

   return mlir::asMainReturnCode(
	mlir::MlirOptMain(argc, argv, "Protocol Emulator Compiler\n", registry));
}

