(** Handwritten lexer for protocol-emulator assembly ([.peas]). *)

type token =
  | Ident of string
  | Int of int
  | Colon
  | Comma
  | Equal
  | Eof

type t = {
  file : string;
  src : string;
  mutable i : int;
  mutable line : int;
  mutable col : int;
  mutable start_line : int;
  mutable start_col : int;
}

let create ~file src =
  { file; src; i = 0; line = 1; col = 1; start_line = 1; start_col = 1 }

let loc lex = Loc.make ~file:lex.file ~line:lex.start_line ~col:lex.start_col

let peek lex =
  if lex.i >= String.length lex.src then None else Some lex.src.[lex.i]

let bump lex =
  match peek lex with
  | None -> ()
  | Some '\n' ->
      lex.i <- lex.i + 1;
      lex.line <- lex.line + 1;
      lex.col <- 1
  | Some _ ->
      lex.i <- lex.i + 1;
      lex.col <- lex.col + 1

let rec skip_ws lex =
  match peek lex with
  | Some (' ' | '\t' | '\r' | '\n') ->
      bump lex;
      skip_ws lex
  | Some ';' ->
      while
        match peek lex with
        | Some '\n' | None -> false
        | Some _ ->
            bump lex;
            true
      do
        ()
      done;
      skip_ws lex
  | _ -> ()

let is_ident_start = function 'A' .. 'Z' | 'a' .. 'z' | '_' -> true | _ -> false
let is_ident_cont = function 'A' .. 'Z' | 'a' .. 'z' | '0' .. '9' | '_' -> true | _ -> false
let is_digit = function '0' .. '9' -> true | _ -> false
let is_hex = function '0' .. '9' | 'a' .. 'f' | 'A' .. 'F' -> true | _ -> false

let take_while lex pred =
  let buf = Buffer.create 16 in
  while match peek lex with Some c when pred c -> true | _ -> false do
    Buffer.add_char buf (Option.get (peek lex));
    bump lex
  done;
  Buffer.contents buf

let next lex =
  skip_ws lex;
  lex.start_line <- lex.line;
  lex.start_col <- lex.col;
  match peek lex with
  | None -> Eof
  | Some ':' ->
      bump lex;
      Colon
  | Some ',' ->
      bump lex;
      Comma
  | Some '=' ->
      bump lex;
      Equal
  | Some '0' when lex.i + 1 < String.length lex.src && (lex.src.[lex.i + 1] = 'x' || lex.src.[lex.i + 1] = 'X')
    ->
      bump lex;
      bump lex;
      let digits = take_while lex is_hex in
      if digits = "" then failwith (Loc.to_string (loc lex) ^ ": expected hex digits")
      else Int (int_of_string ("0x" ^ digits))
  | Some c when is_digit c ->
      let digits = take_while lex is_digit in
      Int (int_of_string digits)
  | Some c when is_ident_start c ->
      let id = take_while lex is_ident_cont in
      Ident id
  | Some c ->
      failwith
        (Printf.sprintf "%s: unexpected character %C" (Loc.to_string (loc lex)) c)
