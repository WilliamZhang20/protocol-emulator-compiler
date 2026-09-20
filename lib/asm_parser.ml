(** Recursive-descent parser for protocol-emulator assembly. *)

type t = {
  lex : Asm_lexer.t;
  mutable tok : Asm_lexer.token;
  mutable tok_loc : Loc.t;
  mutable peeked : (Asm_lexer.token * Loc.t) option;
}

let pull lex =
  let tok = Asm_lexer.next lex in
  (tok, Asm_lexer.loc lex)

let advance p =
  match p.peeked with
  | Some (tok, loc) ->
      p.peeked <- None;
      p.tok <- tok;
      p.tok_loc <- loc
  | None ->
      let tok, loc = pull p.lex in
      p.tok <- tok;
      p.tok_loc <- loc

let lookahead p =
  match p.peeked with
  | Some (tok, _) -> tok
  | None ->
      let tok, loc = pull p.lex in
      p.peeked <- Some (tok, loc);
      tok

let create ~file src =
  let lex = Asm_lexer.create ~file src in
  let p = { lex; tok = Asm_lexer.Eof; tok_loc = Loc.dummy; peeked = None } in
  advance p;
  p

let error p msg = raise (Failure (Error.to_string (Error.make ~loc:p.tok_loc msg)))

let expect_ident p =
  match p.tok with
  | Asm_lexer.Ident s ->
      advance p;
      s
  | _ -> error p "expected identifier"

let expect_comma p =
  match p.tok with
  | Asm_lexer.Comma -> advance p
  | _ -> error p "expected ','"

let peek_ident p = match p.tok with Asm_lexer.Ident s -> Some s | _ -> None

let operand p =
  match p.tok with
  | Asm_lexer.Int n ->
      advance p;
      Asm_ast.Imm n
  | Asm_lexer.Ident s ->
      advance p;
      let lower = String.lowercase_ascii s in
      if String.length lower = 2 && lower.[0] = 'r' && lower.[1] >= '0' && lower.[1] <= '7'
      then Asm_ast.Reg (Char.code lower.[1] - Char.code '0')
      else (
        match Isa.event_of_name s with
        | Some ev -> Asm_ast.Event ev
        | None -> (
            match Isa.action_op_of_name s with
            | Some op -> Asm_ast.Action_op op
            | None -> Asm_ast.Label s))
  | _ -> error p "expected operand (number, register, label, or named constant)"

(* Statements are newline-insensitive. A label is [ident ':']. An instruction
   is [ident args...]. After an ident we look ahead: colon means label. *)

let looks_like_kv p =
  match p.tok with
  | Asm_lexer.Ident _ -> (
      match lookahead p with Asm_lexer.Equal -> true | _ -> false)
  | _ -> false

let parse_kv_list p =
  let rec loop acc =
    if not (looks_like_kv p) then List.rev acc
    else
      let key = expect_ident p in
      (match p.tok with Asm_lexer.Equal -> advance p | _ -> error p "expected '='");
      let v = operand p in
      let acc = (String.lowercase_ascii key, v) :: acc in
      match p.tok with
      | Asm_lexer.Comma ->
          expect_comma p;
          loop acc
      | _ -> List.rev acc
  in
  loop []

let find_kv kvs key =
  try Some (List.assoc key kvs) with Not_found -> None

let require_kv kvs key =
  match find_kv kvs key with
  | Some v -> v
  | None -> failwith ("missing keyword argument '" ^ key ^ "'")

let bool_of_operand = function
  | Asm_ast.Imm 0 -> false
  | Asm_ast.Imm _ -> true
  | Asm_ast.Label s ->
      (match String.lowercase_ascii s with
      | "true" | "yes" | "on" -> true
      | "false" | "no" | "off" -> false
      | _ -> failwith ("expected boolean, got " ^ s))
  | _ -> failwith "expected boolean operand"

let int_of_imm = function
  | Asm_ast.Imm n -> n
  | _ -> failwith "expected immediate integer"

let mnemonic_of_name name =
  match String.lowercase_ascii name with
  | "nop" -> `Nullary Asm_ast.Nop
  | "halt" -> `Nullary Asm_ast.Halt
  | "tx_load" -> `Nullary Asm_ast.Tx_load
  | "rx_push" -> `Nullary Asm_ast.Rx_push
  | "shift_clear" -> `Nullary Asm_ast.Shift_clear
  | "event_stamp" -> `Nullary Asm_ast.Event_stamp
  | "crc_finalize" -> `Nullary Asm_ast.Crc_finalize
  | "crc_push_lo" -> `Nullary Asm_ast.Crc_push_lo
  | "crc_push_hi" -> `Nullary Asm_ast.Crc_push_hi
  | "crc32_setup" -> `Nullary Asm_ast.Crc32_setup
  | "crc_push_b2" -> `Nullary Asm_ast.Crc_push_b2
  | "crc_push_b3" -> `Nullary Asm_ast.Crc_push_b3
  | "line_release" -> `Nullary Asm_ast.Line_release
  | "line_sample" -> `Nullary Asm_ast.Line_sample
  | "wait_region" -> `Nullary Asm_ast.Wait_region
  | "wait16" | "wait" -> `Unary (fun a -> Asm_ast.Wait16 a)
  | "start_timer" -> `Unary (fun a -> Asm_ast.Start_timer a)
  | "wait_event" -> `Unary (fun a -> Asm_ast.Wait_event a)
  | "shift_out" -> `Unary (fun a -> Asm_ast.Shift_out a)
  | "shift_in" -> `Unary (fun a -> Asm_ast.Shift_in a)
  | "jmp" | "jump" -> `Unary (fun a -> Asm_ast.Jmp a)
  | "jz" -> `Unary (fun a -> Asm_ast.Jz a)
  | "jnz" -> `Unary (fun a -> Asm_ast.Jnz a)
  | "get_time" -> `Unary (fun a -> Asm_ast.Get_time a)
  | "wait_until" -> `Unary (fun a -> Asm_ast.Wait_until a)
  | "crc_feed" -> `Unary (fun a -> Asm_ast.Crc_feed a)
  | "line_drive" -> `Unary (fun a -> Asm_ast.Line_drive a)
  | "run_region" -> `Unary (fun a -> Asm_ast.Run_region a)
  | "read_result" -> `Unary (fun a -> Asm_ast.Read_result a)
  | "action_load_shift" -> `Unary (fun a -> Asm_ast.Action_load_shift a)
  | "gpio_write" -> `Binary (fun a b -> Asm_ast.Gpio_write { pin = a; value = b })
  | "gpio_oe" -> `Binary (fun a b -> Asm_ast.Gpio_oe { pin = a; enabled = b })
  | "wait_pin" -> `Binary (fun a b -> Asm_ast.Wait_pin { pin = a; value = b })
  | "sideset" -> `Binary (fun a b -> Asm_ast.Sideset { pin = a; value = b })
  | "map_pin" -> `Binary (fun a b -> Asm_ast.Map_pin { logical = a; physical = b })
  | "set" -> `Binary (fun a b -> Asm_ast.Set { reg = a; imm = b })
  | "mov" -> `Binary (fun a b -> Asm_ast.Mov { dst = a; src = b })
  | "djnz" -> `Binary (fun a b -> Asm_ast.Djnz { reg = a; target = b })
  | "add" -> `Alu Isa.Add
  | "sub" -> `Alu Isa.Sub
  | "and" -> `Alu Isa.And
  | "or" -> `Alu Isa.Or
  | "xor" -> `Alu Isa.Xor
  | "shl" -> `Alu Isa.Shl
  | "shr" -> `Alu Isa.Shr
  | "arm_edge" | "arm_edges" -> `Arm
  | "run_region_n" -> `Binary (fun a b -> Asm_ast.Run_region_n { slot = a; count = b })
  | "action_wr_lo" -> `Binary (fun a b -> Asm_ast.Action_wr_lo { slot = a; data = b })
  | "action_wr_hi" -> `Binary (fun a b -> Asm_ast.Action_wr_hi { slot = a; data = b })
  | "prog_action" -> `Binary (fun a b -> Asm_ast.Prog_action { slot = a; word = b })
  | "action_word" -> `Binary (fun a b -> Asm_ast.Action_word { op = a; args = b })
  | "line_cfg" -> `Line_cfg
  | "crc_setup" -> `Crc_setup
  | "start_xfer" -> `Start_xfer
  | other -> `Unknown other

let parse_instr p loc name =
  match mnemonic_of_name name with
  | `Nullary op -> Asm_ast.Instr { loc; op }
  | `Unary f ->
      let a = operand p in
      Asm_ast.Instr { loc; op = f a }
  | `Binary f ->
      let a = operand p in
      expect_comma p;
      let b = operand p in
      Asm_ast.Instr { loc; op = f a b }
  | `Alu alu ->
      let dst = operand p in
      expect_comma p;
      let src = operand p in
      Asm_ast.Instr { loc; op = Asm_ast.Alu { op = alu; dst; src } }
  | `Arm ->
      let rise = operand p in
      expect_comma p;
      let fall = operand p in
      let compare =
        match p.tok with
        | Asm_lexer.Comma ->
            expect_comma p;
            bool_of_operand (operand p)
        | _ -> false
      in
      Asm_ast.Instr { loc; op = Asm_ast.Arm_edges { rise; fall; compare } }
  | `Line_cfg ->
      let pin_a = operand p in
      expect_comma p;
      let pin_b = operand p in
      let jk_swap =
        match p.tok with
        | Asm_lexer.Comma ->
            expect_comma p;
            bool_of_operand (operand p)
        | _ -> false
      in
      Asm_ast.Instr { loc; op = Asm_ast.Line_cfg { pin_a; pin_b; jk_swap } }
  | `Crc_setup ->
      let kvs = parse_kv_list p in
      if kvs = [] then (
        (* positional: width, poly [, refin, refout, xor_ones, init_ones] *)
        let width = operand p in
        expect_comma p;
        let poly = operand p in
        Asm_ast.Instr
          {
            loc;
            op =
              Asm_ast.Crc_setup
                {
                  width;
                  poly;
                  refin = true;
                  refout = true;
                  xor_ones = true;
                  init_ones = true;
                };
          })
      else
        Asm_ast.Instr
          {
            loc;
            op =
              Asm_ast.Crc_setup
                {
                  width = require_kv kvs "width";
                  poly = require_kv kvs "poly";
                  refin =
                    (match find_kv kvs "refin" with
                    | Some v -> bool_of_operand v
                    | None -> true);
                  refout =
                    (match find_kv kvs "refout" with
                    | Some v -> bool_of_operand v
                    | None -> true);
                  xor_ones =
                    (match find_kv kvs "xor_ones" with
                    | Some v -> bool_of_operand v
                    | None -> true);
                  init_ones =
                    (match find_kv kvs "init_ones" with
                    | Some v -> bool_of_operand v
                    | None -> true);
                };
          }
  | `Start_xfer ->
      let kvs = parse_kv_list p in
      let get key alt =
        match find_kv kvs key with
        | Some v -> v
        | None -> (
            match alt with
            | Some k -> require_kv kvs k
            | None -> failwith ("start_xfer missing '" ^ key ^ "'"))
      in
      let flag key default =
        match find_kv kvs key with Some v -> bool_of_operand v | None -> default
      in
      let imm_flag key default =
        match find_kv kvs key with Some v -> int_of_imm v | None -> default
      in
      Asm_ast.Instr
        {
          loc;
          op =
            Asm_ast.Start_xfer
              {
                clk_pin = get "clk" (Some "clk_pin");
                tx_pin = get "tx" (Some "tx_pin");
                rx_pin = get "rx" (Some "rx_pin");
                bit_count = get "bits" (Some "bit_count");
                half_period = get "half" (Some "half_period");
                msb_first = flag "msb_first" true;
                clk_idle = imm_flag "clk_idle" 0;
                sample_phase = imm_flag "sample_phase" 0;
                tx_open_drain = flag "tx_od" false;
                clk_open_drain = flag "clk_od" false;
                wait_clk_high = flag "wait_clk_high" false;
              };
        }
  | `Unknown other -> error p ("unknown mnemonic '" ^ other ^ "'")

let parse_program ~file src =
  let p = create ~file src in
  let rec loop acc =
    match p.tok with
    | Asm_lexer.Eof -> List.rev acc
    | Asm_lexer.Ident name ->
        let loc = p.tok_loc in
        advance p;
        (match p.tok with
        | Asm_lexer.Colon ->
            advance p;
            loop (Asm_ast.Label_def { loc; name } :: acc)
        | _ -> loop (parse_instr p loc name :: acc))
    | _ -> error p "expected label or mnemonic"
  in
  { Asm_ast.file; stmts = loop [] }
