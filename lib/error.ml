(** Shared error type for the assembler and frontend. *)

type t = { loc : Loc.t option; message : string }

let make ?loc message = { loc; message }

let to_string err =
  match err.loc with
  | None -> err.message
  | Some loc -> Printf.sprintf "%s: %s" (Loc.to_string loc) err.message
