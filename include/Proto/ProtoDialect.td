include "mlir/IR/Dialectbase.td"

def Proto_Dialect : Diaelect {
   let name = "proto";
   let cppNamespace = "::proto";
   
   let summary = "Protocol description dialect";

   let description = [{ 
       protocol-level operations before lowering to the ASIC
   }];
}


