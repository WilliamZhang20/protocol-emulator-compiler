(** Source locations for assembler and frontend diagnostics. *)

type t = { file : string; line : int; col : int }

let dummy = { file = "<unknown>"; line = 0; col = 0 }

let make ~file ~line ~col = { file; line; col }

let to_string loc =
  Printf.sprintf "%s:%d:%d" loc.file loc.line loc.col
