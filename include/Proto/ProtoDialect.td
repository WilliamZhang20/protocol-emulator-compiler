include "mlir/IR/DialectBase.td"

def Proto_Dialect : Dialect {
   let name = "proto";
   let cppNamespace = "::proto";
   
   let summary = "Protocol description dialect";

   let description = [{ 
       protocol-level operations before lowering to the ASIC
   }];
}


