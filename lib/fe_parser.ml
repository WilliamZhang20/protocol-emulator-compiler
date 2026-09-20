(** Recursive-descent parser for the protocol-emulator frontend language. *)

type t = {
  lex : Fe_lexer.t;
  mutable tok : Fe_lexer.token;
  mutable tok_loc : Loc.t;
}

let advance p =
  p.tok_loc <- Fe_lexer.loc p.lex;
  p.tok <- Fe_lexer.next p.lex

let create ~file src =
  let lex = Fe_lexer.create ~file src in
  let p = { lex; tok = Fe_lexer.Eof; tok_loc = Loc.dummy } in
  advance p;
  p

let error p msg = raise (Failure (Error.to_string (Error.make ~loc:p.tok_loc msg)))

let expect p tok msg =
  if p.tok = tok then advance p else error p msg

let expect_ident p =
  match p.tok with
  | Fe_lexer.Ident s ->
      advance p;
      s
  | _ -> error p "expected identifier"

let expect_int p =
  match p.tok with
  | Fe_lexer.Int n ->
      advance p;
      n
  | _ -> error p "expected integer"

let expr p =
  match p.tok with
  | Fe_lexer.Int n ->
      advance p;
      Fe_ast.Int n
  | Fe_lexer.Ident s ->
      advance p;
      Fe_ast.Name s
  | _ -> error p "expected expression"

let peek_ident p = match p.tok with Fe_lexer.Ident s -> Some s | _ -> None

let call_args p =
  expect p Fe_lexer.Lparen "expected '('";
  if p.tok = Fe_lexer.Rparen then (
    advance p;
    [])
  else
    let rec rest acc =
      match p.tok with
      | Fe_lexer.Comma ->
          advance p;
          rest (expr p :: acc)
      | Fe_lexer.Rparen ->
          advance p;
          List.rev acc
      | _ -> error p "expected ',' or ')'"
    in
    rest [ expr p ]

let eat_semi p =
  match p.tok with Fe_lexer.Semi -> advance p | _ -> ()

let rec block p =
  expect p Fe_lexer.Lbrace "expected '{'";
  let rec loop acc =
    match p.tok with
    | Fe_lexer.Rbrace ->
        advance p;
        List.rev acc
    | Fe_lexer.Eof -> error p "unclosed '{'"
    | _ -> loop (stmt p :: acc)
  in
  loop []

and stmt p =
  match p.tok with
  | Fe_lexer.Ident name ->
      let loc_name = String.lowercase_ascii name in
      advance p;
      if p.tok = Fe_lexer.Colon then (
        advance p;
        Fe_ast.Label name)
      else
        let s = parse_named loc_name p in
        eat_semi p;
        s
  | _ -> error p "expected statement"

and parse_named name p =
  match name with
  | "loop" -> Fe_ast.Loop (block p)
  | "repeat" ->
      expect p Fe_lexer.Lparen "expected '(' after repeat";
      let n = expr p in
      expect p Fe_lexer.Rparen "expected ')'";
      Fe_ast.Repeat (n, block p)
  | "region" ->
      expect p Fe_lexer.Lparen "expected '(' after region";
      let slot = expr p in
      expect p Fe_lexer.Rparen "expected ')'";
      expect p Fe_lexer.Lbrace "expected '{' after region";
      let rec acts acc =
        match p.tok with
        | Fe_lexer.Rbrace ->
            advance p;
            List.rev acc
        | Fe_lexer.Ident "action" ->
            advance p;
            let a = parse_action p in
            eat_semi p;
            acts (a :: acc)
        | _ -> error p "expected 'action' or '}'"
      in
      Fe_ast.Region { slot; actions = acts [] }
  | "goto" -> Fe_ast.Goto (expect_ident p)
  | "halt" -> Fe_ast.Halt
  | "tx_load" -> Fe_ast.Tx_load
  | "rx_push" -> Fe_ast.Rx_push
  | "shift_clear" -> Fe_ast.Shift_clear
  | "wait_region" -> Fe_ast.Wait_region
  | "gpio_oe" -> (
      match call_args p with
      | [ a; b ] -> Fe_ast.Gpio_oe (a, b)
      | _ -> error p "gpio_oe(pin, enabled)")
  | "gpio_write" -> (
      match call_args p with
      | [ a; b ] -> Fe_ast.Gpio_write (a, b)
      | _ -> error p "gpio_write(pin, value)")
  | "shift_out" -> (
      match call_args p with
      | [ a ] -> Fe_ast.Shift_out a
      | _ -> error p "shift_out(pin)")
  | "shift_in" -> (
      match call_args p with
      | [ a ] -> Fe_ast.Shift_in a
      | _ -> error p "shift_in(pin)")
  | "wait16" | "wait" -> (
      match call_args p with
      | [ a ] -> Fe_ast.Wait16 a
      | _ -> error p "wait16(cycles)")
  | "wait_pin" -> (
      match call_args p with
      | [ a; b ] -> Fe_ast.Wait_pin (a, b)
      | _ -> error p "wait_pin(pin, value)")
  | "wait_event" -> (
      match call_args p with
      | [ a ] -> Fe_ast.Wait_event a
      | _ -> error p "wait_event(mask)")
  | "start_timer" -> (
      match call_args p with
      | [ a ] -> Fe_ast.Start_timer a
      | _ -> error p "start_timer(cycles)")
  | "set" -> (
      match call_args p with
      | [ Fe_ast.Name r; e ] -> Fe_ast.Set (r, e)
      | _ -> error p "set(rN, imm)")
  | "mov" -> (
      match call_args p with
      | [ Fe_ast.Name d; Fe_ast.Name s ] -> Fe_ast.Mov (d, s)
      | _ -> error p "mov(rd, rs)")
  | "add" | "sub" | "and" | "or" | "xor" | "shl" | "shr" -> (
      let alu =
        match name with
        | "add" -> Isa.Add
        | "sub" -> Isa.Sub
        | "and" -> Isa.And
        | "or" -> Isa.Or
        | "xor" -> Isa.Xor
        | "shl" -> Isa.Shl
        | "shr" -> Isa.Shr
        | _ -> assert false
      in
      match call_args p with
      | [ Fe_ast.Name d; Fe_ast.Name s ] -> Fe_ast.Alu (alu, d, s)
      | _ -> error p (name ^ "(rd, rs)"))
  | "run_region" -> (
      match call_args p with
      | [ a ] -> Fe_ast.Run_region a
      | _ -> error p "run_region(slot)")
  | "read_result" -> (
      match call_args p with
      | [ Fe_ast.Name r ] -> Fe_ast.Read_result r
      | _ -> error p "read_result(rN)")
  | other -> error p ("unknown statement '" ^ other ^ "'")

and parse_action p =
  let kind = String.lowercase_ascii (expect_ident p) in
  let args = call_args p in
  match kind, args with
  | "gpio", [ pin ] -> Fe_ast.Act_gpio { pin; out = None; oe = None }
  | "gpio", [ pin; out ] -> Fe_ast.Act_gpio { pin; out = Some out; oe = None }
  | "gpio", [ pin; out; oe ] ->
      Fe_ast.Act_gpio { pin; out = Some out; oe = Some oe }
  | "sample", [ pin ] -> Fe_ast.Act_sample pin
  | "shift", [ pin ] ->
      Fe_ast.Act_shift { pin; shift_in = false; msb_first = false }
  | "delay", [ n ] -> Fe_ast.Act_delay n
  | "done", [] -> Fe_ast.Act_done
  | "count_load", [ n ] -> Fe_ast.Act_count_load n
  | "count_djnz", [ s ] -> Fe_ast.Act_count_djnz s
  | _ ->
      error p
        "action gpio/sample/shift/delay/done/count_load/count_djnz (see \
         architecture.md Phase C)"

let decl p =
  match peek_ident p with
  | Some "pin" ->
      advance p;
      let name = expect_ident p in
      expect p Fe_lexer.Equal "expected '='";
      let physical = expect_int p in
      eat_semi p;
      Fe_ast.Pin { name; physical }
  | Some "imm" ->
      advance p;
      let name = expect_ident p in
      expect p Fe_lexer.Equal "expected '='";
      let value = expect_int p in
      eat_semi p;
      Fe_ast.Imm { name; value }
  | _ -> error p "expected 'pin' or 'imm' declaration"

let engine p =
  (match peek_ident p with
  | Some s when String.lowercase_ascii s = "engine" -> advance p
  | _ -> error p "expected 'engine'");
  let name = expect_ident p in
  expect p Fe_lexer.Lbrace "expected '{'";
  let rec loop decls body =
    match p.tok with
    | Fe_lexer.Rbrace ->
        advance p;
        { Fe_ast.name; decls = List.rev decls; body = List.rev body }
    | Fe_lexer.Ident s when s = "pin" || s = "imm" ->
        loop (decl p :: decls) body
    | Fe_lexer.Eof -> error p "unclosed engine"
    | _ -> loop decls (stmt p :: body)
  in
  loop [] []

let parse_program ~file src =
  let p = create ~file src in
  let rec loop acc =
    match p.tok with
    | Fe_lexer.Eof -> List.rev acc
    | _ -> loop (engine p :: acc)
  in
  { Fe_ast.file; engines = loop [] }
