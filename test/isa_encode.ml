(** Encoding checks against protocol-emulator [programs.py] helpers. *)

open Pec

let require cond msg =
  if not cond then (
    prerr_endline ("FAIL: " ^ msg);
    exit 1)

let eq_list got want label =
  require (got = want)
    (Printf.sprintf "%s: got [%s] want [%s]" label
       (String.concat " " (List.map (Printf.sprintf "%02x") got))
       (String.concat " " (List.map (Printf.sprintf "%02x") want)))

let () =
  require (Isa.sram_size = 1024) "SRAM is 1024x8";
  require (Isa.rf_count = 8) "8x16 register file";
  require (Isa.nop = 0x00 && Isa.halt = 0x01) "NOP/HALT";
  require (Isa.tx_load = 0x40 && Isa.rx_push = 0x70) "TX_LOAD/RX_PUSH";
  require (Isa.encode_gpio_write ~pin:0 ~value:1 = 0x28) "gpio_write(0,1)";
  require (Isa.encode_gpio_oe ~pin:0 ~enabled:1 = 0x38) "gpio_oe(0,1)";
  eq_list (Isa.encode_wait16 423) [ 0x10; 423 land 0xFF; 423 lsr 8 ] "WAIT16 423";
  eq_list (Isa.encode_jump 5) [ 0x80; 0x05; 0x00 ] "JMP 5";
  eq_list (Isa.encode_set ~reg:0 ~imm8:3) [ 0xAA; 0x00; 0x03 ] "SET r0, 3";
  eq_list (Isa.encode_mov ~dst:2 ~src:0) [ 0xAB; 0x02 ] "MOV r2, r0";
  eq_list
    (Isa.encode_alu ~op:Isa.Sub ~dst:2 ~src:1)
    [ 0xAC; 0x01; 0x0A ] "SUB r2, r1";
  eq_list (Isa.encode_djnz ~reg:0 ~addr:5) [ 0x88; 0x05; 0x00 ] "DJNZ r0, 5";
  require (Isa.event_mask Isa.Ev_region_done = 1 lsl 6) "EV_REGION_DONE";
  require (Isa.action_word ~op:7 ~args:4 = 0x7004) "ACT_DELAY 4";
  eq_list
    (Isa.encode_prog_action ~slot:0 ~word:0x7004)
    [ 0xE8; 0; 0x04; 0xE9; 0; 0x70 ]
    "prog_action DELAY";

  (* Assemble the ALU smoke program from test_alu_branch.py (DJNZ loop). *)
  let src =
    {|
      gpio_oe 4, 1
      gpio_write 4, 0
      set r0, 3
    loop:
      gpio_write 4, 1
      gpio_write 4, 0
      djnz r0, loop
      halt
    |}
  in
  match Assemble.assemble_source ~file:"<test>" src with
  | Error err ->
      prerr_endline (Error.to_string err);
      exit 1
  | Ok bytes ->
      require (List.hd bytes = 0x3C) "gpio_oe 4,1";
      require (List.length bytes < Isa.sram_size) "fits in SRAM";
      print_endline "isa_encode: ok"
