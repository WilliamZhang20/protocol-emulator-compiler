(** Two-pass assembler: labels then SRAM bytes.

    Output is a protocol-emulator program image (max [Isa.sram_size] bytes).
    Host nibble loading (commands 1–5) is intentionally not implemented here —
    that belongs with a future FPGA/host loader, not this frontend. *)

open Asm_ast

let fail ?loc message = Error (Error.make ?loc message)

let ( let* ) = Result.bind

let as_int ~loc what = function
  | Imm n -> Ok n
  | Reg n -> Ok n
  | Event ev -> Ok (Isa.event_mask ev)
  | Action_op op -> Ok (Isa.action_op_code op)
  | Label name ->
      fail ~loc (Printf.sprintf "%s: expected immediate, got label '%s'" what name)

let as_reg ~loc op =
  let r =
    match op with
    | Reg n | Imm n -> Isa.check_reg n
    | _ -> Error "expected register r0..r7"
  in
  Result.map_error (fun message -> Error.make ~loc message) r

let as_pin ~loc what op =
  let* n = as_int ~loc what op in
  Isa.check_pin n |> Result.map_error (fun message -> Error.make ~loc message)

let resolve ~labels ~loc = function
  | Label name -> (
      match List.assoc_opt name labels with
      | Some addr -> Ok addr
      | None -> fail ~loc (Printf.sprintf "unknown label '%s'" name))
  | Imm n -> Ok n
  | Reg n -> Ok n
  | Event ev -> Ok (Isa.event_mask ev)
  | Action_op op -> Ok (Isa.action_op_code op)

let encode_op ~labels ~loc op =
  let imm what operand =
    let* n = resolve ~labels ~loc operand in
    as_int ~loc what (Imm n)
  in
  let pin_of what operand = as_pin ~loc what operand in
  let addr operand =
    let* n = resolve ~labels ~loc operand in
    Isa.check_addr n |> Result.map_error (fun message -> Error.make ~loc message)
  in
  match op with
  | Nop -> Ok [ Isa.nop ]
  | Halt -> Ok [ Isa.halt ]
  | Tx_load -> Ok [ Isa.tx_load ]
  | Rx_push -> Ok [ Isa.rx_push ]
  | Shift_clear -> Ok [ Isa.shift_clear ]
  | Event_stamp -> Ok [ Isa.event_stamp ]
  | Crc_finalize -> Ok [ Isa.crc_finalize ]
  | Crc_push_lo -> Ok [ Isa.crc_push_lo ]
  | Crc_push_hi -> Ok [ Isa.crc_push_hi ]
  | Crc32_setup -> Ok [ Isa.crc32_setup ]
  | Crc_push_b2 -> Ok [ Isa.crc_push_b2 ]
  | Crc_push_b3 -> Ok [ Isa.crc_push_b3 ]
  | Line_release -> Ok [ Isa.line_release ]
  | Line_sample -> Ok [ Isa.line_sample ]
  | Wait_region -> Ok [ Isa.wait_region ]
  | Wait16 n ->
      let* cycles = imm "WAIT16" n in
      let* cycles =
        Isa.check_u16 "WAIT16" cycles
        |> Result.map_error (fun message -> Error.make ~loc message)
      in
      Ok (Isa.encode_wait16 cycles)
  | Start_timer n ->
      let* cycles = imm "START_TIMER" n in
      Ok (Isa.encode_start_timer cycles)
  | Wait_event n ->
      let* mask = resolve ~labels ~loc n in
      Ok (Isa.encode_wait_event mask)
  | Arm_edges { rise; fall; compare } ->
      let* rise = imm "ARM_EDGE rise" rise in
      let* fall = imm "ARM_EDGE fall" fall in
      Ok (Isa.encode_arm_edges ~rise ~fall ~compare)
  | Gpio_write { pin; value } ->
      let* pin = pin_of "GPIO_WRITE" pin in
      let* value = imm "GPIO_WRITE value" value in
      Ok [ Isa.encode_gpio_write ~pin ~value ]
  | Gpio_oe { pin; enabled } ->
      let* pin = pin_of "GPIO_OE" pin in
      let* enabled = imm "GPIO_OE" enabled in
      Ok [ Isa.encode_gpio_oe ~pin ~enabled ]
  | Shift_out p ->
      let* pin = pin_of "SHIFT_OUT" p in
      Ok [ Isa.encode_shift_out ~pin ]
  | Shift_in p ->
      let* pin = pin_of "SHIFT_IN" p in
      Ok [ Isa.encode_shift_in ~pin ]
  | Jmp t ->
      let* addr = addr t in
      Ok (Isa.encode_jump addr)
  | Jz t ->
      let* addr = addr t in
      Ok (Isa.encode_jz addr)
  | Jnz t ->
      let* addr = addr t in
      Ok (Isa.encode_jnz addr)
  | Djnz { reg; target } ->
      let* reg = as_reg ~loc reg in
      let* addr = addr target in
      Ok (Isa.encode_djnz ~reg ~addr)
  | Set { reg; imm = v } ->
      let* reg = as_reg ~loc reg in
      let* imm8 = imm "SET" v in
      let* imm8 =
        Isa.check_u8 "SET imm" imm8
        |> Result.map_error (fun message -> Error.make ~loc message)
      in
      Ok (Isa.encode_set ~reg ~imm8)
  | Mov { dst; src } ->
      let* dst = as_reg ~loc dst in
      let* src = as_reg ~loc src in
      Ok (Isa.encode_mov ~dst ~src)
  | Alu { op; dst; src } ->
      let* dst = as_reg ~loc dst in
      let* src = as_reg ~loc src in
      Ok (Isa.encode_alu ~op ~dst ~src)
  | Get_time r ->
      let* reg = as_reg ~loc r in
      Ok (Isa.encode_get_time ~reg)
  | Wait_until r ->
      let* reg = as_reg ~loc r in
      Ok (Isa.encode_wait_until ~reg)
  | Wait_pin { pin; value } ->
      let* pin = pin_of "WAIT_PIN" pin in
      let* value = imm "WAIT_PIN" value in
      Ok [ Isa.encode_wait_pin ~pin ~value ]
  | Sideset { pin; value } ->
      let* pin = pin_of "SIDESET" pin in
      let* value = imm "SIDESET" value in
      Ok (Isa.encode_sideset ~pin ~value)
  | Map_pin { logical; physical } ->
      let* logical = pin_of "MAP logical" logical in
      let* physical = pin_of "MAP physical" physical in
      Ok (Isa.encode_map_pin ~logical ~physical)
  | Crc_setup { width; poly; refin; refout; xor_ones; init_ones } ->
      let* width = imm "CRC_SETUP width" width in
      let* poly = imm "CRC_SETUP poly" poly in
      Ok (Isa.encode_crc_setup ~width ~poly ~refin ~refout ~xor_ones ~init_ones)
  | Crc_feed b ->
      let* byte = imm "CRC_FEED" b in
      Ok (Isa.encode_crc_feed byte)
  | Line_cfg { pin_a; pin_b; jk_swap } ->
      let* pin_a = pin_of "LINE_CFG" pin_a in
      let* pin_b = pin_of "LINE_CFG" pin_b in
      Ok (Isa.encode_line_cfg ~pin_a ~pin_b ~jk_swap)
  | Line_drive s ->
      let* state = imm "LINE_DRIVE" s in
      Ok (Isa.encode_line_drive state)
  | Start_xfer x ->
      let* clk_pin = pin_of "START_XFER clk" x.clk_pin in
      let* tx_pin = pin_of "START_XFER tx" x.tx_pin in
      let* rx_pin = pin_of "START_XFER rx" x.rx_pin in
      let* bit_count = imm "START_XFER bits" x.bit_count in
      let* half_period = imm "START_XFER half" x.half_period in
      Ok
        (Isa.encode_start_xfer ~clk_pin ~tx_pin ~rx_pin ~bit_count ~half_period
           ~msb_first:x.msb_first ~clk_idle:x.clk_idle
           ~sample_phase:x.sample_phase ~tx_open_drain:x.tx_open_drain
           ~clk_open_drain:x.clk_open_drain ~wait_clk_high:x.wait_clk_high)
  | Run_region s ->
      let* slot = imm "RUN_REGION" s in
      Ok (Isa.encode_run_region slot)
  | Run_region_n { slot; count } ->
      let* slot = imm "RUN_REGION_N" slot in
      let* count = imm "RUN_REGION_N count" count in
      Ok (Isa.encode_run_region_n ~slot ~count)
  | Read_result r ->
      let* rd = as_reg ~loc r in
      Ok (Isa.encode_read_result rd)
  | Action_wr_lo { slot; data } ->
      let* slot = imm "ACTION_WR_LO" slot in
      let* data = imm "ACTION_WR_LO" data in
      Ok (Isa.encode_action_wr_lo ~slot ~data)
  | Action_wr_hi { slot; data } ->
      let* slot = imm "ACTION_WR_HI" slot in
      let* data = imm "ACTION_WR_HI" data in
      Ok (Isa.encode_action_wr_hi ~slot ~data)
  | Action_load_shift d ->
      let* data = imm "ACTION_LOAD_SHIFT" d in
      Ok (Isa.encode_action_load_shift data)
  | Prog_action { slot; word } ->
      let* slot = imm "prog_action" slot in
      let* word = resolve ~labels ~loc word in
      Ok (Isa.encode_prog_action ~slot ~word)
  | Action_word { op; args } ->
      let* op = resolve ~labels ~loc op in
      let* args = imm "action_word args" args in
      (* Not a CPU opcode — pack the Phase C action word for the caller to
         feed into [prog_action]. Encoding it into SRAM would be meaningless. *)
      fail ~loc
        (Printf.sprintf
           "action_word 0x%04x is a 16-bit action immediate, not a CPU \
            instruction; use `prog_action SLOT, WORD`"
           (Isa.action_word ~op ~args))

let size_of loc op =
  (* First pass uses a dummy label table; jump operands may be labels. *)
  match encode_op ~labels:[] ~loc op with
  | Ok bytes -> Ok (List.length bytes)
  | Error _ -> (
      (* Label not yet resolved: use the architectural size from architecture.md
         (opcode byte + operand bytes). *)
      match op with
      | Nop | Halt | Tx_load | Rx_push | Shift_clear | Event_stamp | Crc_finalize
      | Crc_push_lo | Crc_push_hi | Crc32_setup | Crc_push_b2 | Crc_push_b3
      | Line_release | Line_sample | Wait_region | Gpio_write _ | Gpio_oe _
      | Shift_out _ | Shift_in _ | Wait_pin _ | Sideset _ ->
          Ok 1
      | Wait_event _ | Mov _ | Get_time _ | Wait_until _ | Crc_feed _ | Line_drive _
      | Line_cfg _ | Map_pin _ | Run_region _ | Read_result _ | Action_load_shift _
      ->
          Ok 2
      | Wait16 _ | Start_timer _ | Jmp _ | Jz _ | Jnz _ | Djnz _ | Set _ | Alu _
      | Arm_edges _ | Action_wr_lo _ | Action_wr_hi _ | Run_region_n _ ->
          Ok 3
      | Crc_setup _ | Start_xfer _ -> Ok 4
      | Prog_action _ -> Ok 6
      | Action_word _ -> Ok 0)

let collect_labels program =
  let rec loop addr labels = function
    | [] ->
        if addr > Isa.sram_size then
          fail
            (Printf.sprintf "program is %d bytes; SRAM is %d × 8"
               addr Isa.sram_size)
        else Ok (List.rev labels)
    | Label_def { loc; name } :: rest ->
        if List.mem_assoc name labels then
          fail ~loc (Printf.sprintf "duplicate label '%s'" name)
        else loop addr ((name, addr) :: labels) rest
    | Instr { loc; op } :: rest ->
        let* n = size_of loc op in
        loop (addr + n) labels rest
  in
  loop 0 [] program.stmts

let assemble program =
  let* labels = collect_labels program in
  let rec loop acc = function
    | [] ->
        let bytes = List.concat (List.rev acc) in
        if List.length bytes > Isa.sram_size then
          fail
            (Printf.sprintf "program is %d bytes; SRAM is %d × 8"
               (List.length bytes) Isa.sram_size)
        else Ok bytes
    | Label_def _ :: rest -> loop acc rest
    | Instr { loc; op } :: rest ->
        let* bytes = encode_op ~labels ~loc op in
        loop (bytes :: acc) rest
  in
  loop [] program.stmts

let assemble_source ~file src =
  try
    let program = Asm_parser.parse_program ~file src in
    assemble program
  with Failure msg -> Error (Error.make msg)

let hex_dump ?(origin = 0) bytes =
  let buf = Buffer.create 64 in
  List.iteri
    (fun i b ->
      Buffer.add_string buf (Printf.sprintf "%03x: %02x\n" (origin + i) b))
    bytes;
  Buffer.contents buf

let hex_string bytes =
  String.concat " " (List.map (Printf.sprintf "%02x") bytes)
