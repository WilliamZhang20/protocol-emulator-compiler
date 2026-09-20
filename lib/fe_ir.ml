(** Frontend IR — still symbolic, not SRAM bytecode.

    This is the shape later passes (partitioner / scheduler / codegen
    backend) would consume. Those passes are intentionally not in this
    skeleton. *)

type operand =
  | Imm of int
  | Name of string
  | Reg of int

type inst =
  | Label of string
  | Mnemonic of { name : string; args : operand list }
  | Comment of string

type proc = { name : string; insts : inst list }

type program = { file : string; procs : proc list }

let pp_operand = function
  | Imm n -> string_of_int n
  | Name s -> s
  | Reg n -> Printf.sprintf "r%d" n

let pp_inst = function
  | Label s -> s ^ ":"
  | Mnemonic { name; args } ->
      if args = [] then "  " ^ name
      else
        Printf.sprintf "  %s %s" name
          (String.concat ", " (List.map pp_operand args))
  | Comment s -> "  ; " ^ s

let pp_proc proc =
  Printf.sprintf "; engine %s\n%s\n" proc.name
    (String.concat "\n" (List.map pp_inst proc.insts))

let pp_program program =
  String.concat "\n" (List.map pp_proc program.procs)
