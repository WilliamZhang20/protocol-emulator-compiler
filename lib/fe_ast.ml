(** Compiler-frontend AST.

    This is a thin protocol-oriented source language over the programming
    model in protocol-emulator [docs/architecture.md]: GPIO, WAIT16, FIFOs,
    SHIFT, ALU/RF, events, and Phase C/D action regions.

    TODO: richer declarations (named event masks, action-slot constants).
    TODO: type checking / pin binding validation.
    Codegen to SRAM bytecode is a backend and is out of this skeleton. *)

type ident = string

type expr =
  | Int of int
  | Name of ident

type action =
  | Act_gpio of { pin : expr; out : expr option; oe : expr option }
  | Act_sample of expr
  | Act_shift of { pin : expr; shift_in : bool; msb_first : bool }
  | Act_delay of expr
  | Act_done
  | Act_count_load of expr
  | Act_count_djnz of expr
  (* TODO: ACT_CRC / ACT_NEXT / ACT_REPEAT once the frontend grows. *)

type stmt =
  | Gpio_oe of expr * expr
  | Gpio_write of expr * expr
  | Tx_load
  | Rx_push
  | Shift_out of expr
  | Shift_in of expr
  | Shift_clear
  | Wait16 of expr
  | Wait_pin of expr * expr
  | Wait_event of expr
  | Start_timer of expr
  | Halt
  | Set of ident * expr
  | Mov of ident * ident
  | Alu of Isa.alu_op * ident * ident
  | Loop of stmt list
  | Repeat of expr * stmt list
  | Label of ident
  | Goto of ident
  | Region of { slot : expr; actions : action list }
  | Run_region of expr
  | Wait_region
  | Read_result of ident

type decl =
  | Pin of { name : ident; physical : int }
  | Imm of { name : ident; value : int }

type engine = { name : ident; decls : decl list; body : stmt list }

type program = { file : string; engines : engine list }

let pp_expr = function
  | Int n -> string_of_int n
  | Name s -> s

let pp_alu = function
  | Isa.Add -> "add"
  | Isa.Sub -> "sub"
  | Isa.And -> "and"
  | Isa.Or -> "or"
  | Isa.Xor -> "xor"
  | Isa.Shl -> "shl"
  | Isa.Shr -> "shr"

let rec pp_stmt = function
  | Gpio_oe (p, v) -> Printf.sprintf "gpio_oe(%s, %s);" (pp_expr p) (pp_expr v)
  | Gpio_write (p, v) ->
      Printf.sprintf "gpio_write(%s, %s);" (pp_expr p) (pp_expr v)
  | Tx_load -> "tx_load;"
  | Rx_push -> "rx_push;"
  | Shift_out p -> Printf.sprintf "shift_out(%s);" (pp_expr p)
  | Shift_in p -> Printf.sprintf "shift_in(%s);" (pp_expr p)
  | Shift_clear -> "shift_clear;"
  | Wait16 e -> Printf.sprintf "wait16(%s);" (pp_expr e)
  | Wait_pin (p, v) -> Printf.sprintf "wait_pin(%s, %s);" (pp_expr p) (pp_expr v)
  | Wait_event e -> Printf.sprintf "wait_event(%s);" (pp_expr e)
  | Start_timer e -> Printf.sprintf "start_timer(%s);" (pp_expr e)
  | Halt -> "halt;"
  | Set (r, e) -> Printf.sprintf "set(%s, %s);" r (pp_expr e)
  | Mov (d, s) -> Printf.sprintf "mov(%s, %s);" d s
  | Alu (op, d, s) -> Printf.sprintf "%s(%s, %s);" (pp_alu op) d s
  | Loop body ->
      "loop {\n  " ^ String.concat "\n  " (List.map pp_stmt body) ^ "\n}"
  | Repeat (n, body) ->
      Printf.sprintf "repeat (%s) {\n  %s\n}" (pp_expr n)
        (String.concat "\n  " (List.map pp_stmt body))
  | Label s -> s ^ ":"
  | Goto s -> Printf.sprintf "goto %s;" s
  | Region { slot; actions } ->
      Printf.sprintf "region (%s) { /* %d actions */ }" (pp_expr slot)
        (List.length actions)
  | Run_region e -> Printf.sprintf "run_region(%s);" (pp_expr e)
  | Wait_region -> "wait_region;"
  | Read_result r -> Printf.sprintf "read_result(%s);" r

let pp_engine eng =
  let decls =
    List.map
      (function
        | Pin { name; physical } ->
            Printf.sprintf "  pin %s = %d;" name physical
        | Imm { name; value } -> Printf.sprintf "  imm %s = %d;" name value)
      eng.decls
  in
  let body = List.map (fun s -> "  " ^ pp_stmt s) eng.body in
  Printf.sprintf "engine %s {\n%s\n%s\n}" eng.name
    (String.concat "\n" decls)
    (String.concat "\n" body)

let pp_program program =
  String.concat "\n\n" (List.map pp_engine program.engines) ^ "\n"
