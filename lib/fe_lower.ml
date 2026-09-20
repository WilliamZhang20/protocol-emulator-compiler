(** Lower frontend AST to frontend IR.

    Loops become labels + [jmp]. Constant [repeat n] is unrolled so the IR
    stays explicit. Register names [r0]–[r7] become [Fe_ir.Reg].

    TODO: lower counted loops to [DJNZ Rn] instead of unrolling.
    TODO: resolve named pins / immediates to physical values (partially done).
    TODO: pack Phase C action words ([Isa.action_word]) from [region] bodies.
    TODO: emit assembler AST / bytecode — that is codegen backend, not here. *)

let fresh =
  let n = ref 0 in
  fun prefix ->
    incr n;
    Printf.sprintf ".L%s_%d" prefix !n

let lookup decls name =
  let rec go = function
    | [] -> None
    | Fe_ast.Pin { name = n; physical } :: _ when n = name -> Some physical
    | Fe_ast.Imm { name = n; value } :: _ when n = name -> Some value
    | _ :: rest -> go rest
  in
  go decls

let operand decls = function
  | Fe_ast.Int n -> Fe_ir.Imm n
  | Fe_ast.Name s -> (
      match lookup decls s with
      | Some n -> Fe_ir.Imm n
      | None ->
          let lower = String.lowercase_ascii s in
          if String.length lower = 2 && lower.[0] = 'r' && lower.[1] >= '0'
             && lower.[1] <= '7'
          then Fe_ir.Reg (Char.code lower.[1] - Char.code '0')
          else (
            match Isa.event_of_name s with
            | Some ev -> Fe_ir.Imm (Isa.event_mask ev)
            | None -> Fe_ir.Name s))

let mnemonic name args = Fe_ir.Mnemonic { name; args }

let rec lower_stmt decls stmt acc =
  match stmt with
  | Fe_ast.Gpio_oe (p, v) ->
      mnemonic "gpio_oe" [ operand decls p; operand decls v ] :: acc
  | Fe_ast.Gpio_write (p, v) ->
      mnemonic "gpio_write" [ operand decls p; operand decls v ] :: acc
  | Fe_ast.Tx_load -> mnemonic "tx_load" [] :: acc
  | Fe_ast.Rx_push -> mnemonic "rx_push" [] :: acc
  | Fe_ast.Shift_out p -> mnemonic "shift_out" [ operand decls p ] :: acc
  | Fe_ast.Shift_in p -> mnemonic "shift_in" [ operand decls p ] :: acc
  | Fe_ast.Shift_clear -> mnemonic "shift_clear" [] :: acc
  | Fe_ast.Wait16 e -> mnemonic "wait16" [ operand decls e ] :: acc
  | Fe_ast.Wait_pin (p, v) ->
      mnemonic "wait_pin" [ operand decls p; operand decls v ] :: acc
  | Fe_ast.Wait_event e -> mnemonic "wait_event" [ operand decls e ] :: acc
  | Fe_ast.Start_timer e -> mnemonic "start_timer" [ operand decls e ] :: acc
  | Fe_ast.Halt -> mnemonic "halt" [] :: acc
  | Fe_ast.Set (r, e) ->
      mnemonic "set" [ operand decls (Fe_ast.Name r); operand decls e ] :: acc
  | Fe_ast.Mov (d, s) ->
      mnemonic "mov"
        [ operand decls (Fe_ast.Name d); operand decls (Fe_ast.Name s) ]
      :: acc
  | Fe_ast.Alu (op, d, s) ->
      mnemonic (Fe_ast.pp_alu op)
        [ operand decls (Fe_ast.Name d); operand decls (Fe_ast.Name s) ]
      :: acc
  | Fe_ast.Label s -> Fe_ir.Label s :: acc
  | Fe_ast.Goto s -> mnemonic "jmp" [ Fe_ir.Name s ] :: acc
  | Fe_ast.Wait_region -> mnemonic "wait_region" [] :: acc
  | Fe_ast.Run_region e -> mnemonic "run_region" [ operand decls e ] :: acc
  | Fe_ast.Read_result r ->
      mnemonic "read_result" [ operand decls (Fe_ast.Name r) ] :: acc
  | Fe_ast.Loop body ->
      let l = fresh "loop" in
      let acc = Fe_ir.Label l :: acc in
      let acc = List.fold_left (fun a s -> lower_stmt decls s a) acc body in
      mnemonic "jmp" [ Fe_ir.Name l ] :: acc
  | Fe_ast.Repeat (n, body) -> (
      match operand decls n with
      | Fe_ir.Imm k when k >= 0 && k <= 32 ->
          let rec unroll i acc =
            if i = 0 then acc
            else
              unroll (i - 1)
                (List.fold_left (fun a s -> lower_stmt decls s a) acc body)
          in
          unroll k acc
      | _ ->
          (* TODO: DJNZ Rn lowering for non-constant / large repeats. *)
          Fe_ir.Comment "TODO: lower repeat to DJNZ (codegen/backend)"
          :: List.fold_left (fun a s -> lower_stmt decls s a) acc body)
  | Fe_ast.Region { slot; actions } ->
      let acc =
        Fe_ir.Comment "TODO: pack action words and emit prog_action"
        :: mnemonic "region" [ operand decls slot ]
        :: acc
      in
      List.fold_left
        (fun acc act ->
          match act with
          | Fe_ast.Act_gpio { pin; out; oe } ->
              let args =
                operand decls pin
                :: (match out with None -> [] | Some e -> [ operand decls e ])
                @ match oe with None -> [] | Some e -> [ operand decls e ]
              in
              mnemonic "action_gpio" args :: acc
          | Fe_ast.Act_sample pin ->
              mnemonic "action_sample" [ operand decls pin ] :: acc
          | Fe_ast.Act_shift { pin; shift_in; msb_first } ->
              mnemonic "action_shift"
                [
                  operand decls pin;
                  Fe_ir.Imm (if shift_in then 1 else 0);
                  Fe_ir.Imm (if msb_first then 1 else 0);
                ]
              :: acc
          | Fe_ast.Act_delay e ->
              mnemonic "action_delay" [ operand decls e ] :: acc
          | Fe_ast.Act_done -> mnemonic "action_done" [] :: acc
          | Fe_ast.Act_count_load e ->
              mnemonic "action_count_load" [ operand decls e ] :: acc
          | Fe_ast.Act_count_djnz e ->
              mnemonic "action_count_djnz" [ operand decls e ] :: acc)
        acc actions

let lower_engine (eng : Fe_ast.engine) =
  let insts =
    List.fold_left
      (fun acc s -> lower_stmt eng.decls s acc)
      [] eng.body
    |> List.rev
  in
  { Fe_ir.name = eng.name; insts }

let lower (program : Fe_ast.program) =
  { Fe_ir.file = program.file; procs = List.map lower_engine program.engines }

let parse_and_lower ~file src =
  try
    let ast = Fe_parser.parse_program ~file src in
    Ok (ast, lower ast)
  with Failure msg -> Error (Error.make msg)
