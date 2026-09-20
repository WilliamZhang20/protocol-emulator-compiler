(** Compiler frontend entrypoint: [.pe] → AST / frontend IR.

    Does not emit SRAM bytecode. Use [pec-asm] for handwritten assembly;
    a future codegen backend would lower this IR to the same ISA. *)

let usage =
  "Usage: pec [options] FILE.pe\n\
   \n\
   Protocol-emulator compiler frontend (parse + lower to IR).\n\
   Codegen / partitioner / scheduler are out of scope for this skeleton.\n\
   \n\
   Options:\n\
  \  --dump-ast    print the parsed engine AST\n\
  \  --dump-ir     print frontend IR (default)\n\
  \  --help        show this help\n"

let read_file path =
  let ic = open_in_bin path in
  let len = in_channel_length ic in
  let s = really_input_string ic len in
  close_in ic;
  s

let () =
  let input = ref None in
  let mode = ref `Ir in
  let rec parse = function
    | [] -> ()
    | "-h" :: _ | "--help" :: _ ->
        print_string usage;
        exit 0
    | "--dump-ast" :: rest ->
        mode := `Ast;
        parse rest
    | "--dump-ir" :: rest ->
        mode := `Ir;
        parse rest
    | arg :: _ when String.length arg > 0 && arg.[0] = '-' ->
        prerr_endline ("unknown option: " ^ arg);
        prerr_string usage;
        exit 2
    | arg :: rest ->
        input := Some arg;
        parse rest
  in
  parse (List.tl (Array.to_list Sys.argv));
  match !input with
  | None ->
      prerr_string usage;
      exit 2
  | Some file -> (
      match Pec.Fe_lower.parse_and_lower ~file (read_file file) with
      | Error err ->
          prerr_endline (Pec.Error.to_string err);
          exit 1
      | Ok (ast, ir) -> (
          match !mode with
          | `Ast -> print_string (Pec.Fe_ast.pp_program ast)
          | `Ir -> print_string (Pec.Fe_ir.pp_program ir)))
