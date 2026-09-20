(** Assembler entrypoint: [.peas] → protocol-emulator SRAM bytes. *)

let usage =
  "Usage: pec-asm [options] FILE.peas\n\
   \n\
   Assemble protocol-emulator bytecode (docs/architecture.md ISA).\n\
   \n\
   Options:\n\
  \  -o FILE     write raw bytes\n\
  \  --hex       print address:byte listing (default if no -o)\n\
  \  --flat      print space-separated hex bytes\n\
  \  --help      show this help\n"

let read_file path =
  let ic = open_in_bin path in
  let len = in_channel_length ic in
  let s = really_input_string ic len in
  close_in ic;
  s

let write_bytes path bytes =
  let oc = open_out_bin path in
  List.iter (output_byte oc) bytes;
  close_out oc

let () =
  let input = ref None in
  let output = ref None in
  let mode = ref `Hex in
  let rec parse = function
    | [] -> ()
    | "-h" :: _ | "--help" :: _ ->
        print_string usage;
        exit 0
    | "-o" :: path :: rest ->
        output := Some path;
        parse rest
    | "--hex" :: rest ->
        mode := `Hex;
        parse rest
    | "--flat" :: rest ->
        mode := `Flat;
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
      match Pec.Assemble.assemble_source ~file (read_file file) with
      | Error err ->
          prerr_endline (Pec.Error.to_string err);
          exit 1
      | Ok bytes ->
          Printf.eprintf
            "assembled %d bytes (SRAM capacity %d)\n%!"
            (List.length bytes) Pec.Isa.sram_size;
          (match !output with
          | Some path -> write_bytes path bytes
          | None -> ());
          if !output = None || !mode <> `Hex then
            match !mode with
            | `Hex -> print_string (Pec.Assemble.hex_dump bytes)
            | `Flat ->
                print_endline (Pec.Assemble.hex_string bytes)
          else if !output <> None then ())
