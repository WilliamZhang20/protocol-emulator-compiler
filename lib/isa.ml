(** protocol-emulator SRAM bytecode ISA.

    Encodings are taken from protocol-emulator
    [docs/architecture.md] and [test/cocotb_tests/reference/programs.py]. *)

let sram_size = 1024
let rf_count = 8
let rf_width_bits = 16
let action_slots = 8
let addr_bits = 10

type alu_op = Add | Sub | And | Or | Xor | Shl | Shr

let alu_code = function
  | Add -> 0
  | Sub -> 1
  | And -> 2
  | Or -> 3
  | Xor -> 4
  | Shl -> 5
  | Shr -> 6

type event =
  | Ev_xfer_done
  | Ev_timer_done
  | Ev_pin_rise
  | Ev_pin_fall
  | Ev_compare
  | Ev_line_change
  | Ev_region_done

let event_bit = function
  | Ev_xfer_done -> 0
  | Ev_timer_done -> 1
  | Ev_pin_rise -> 2
  | Ev_pin_fall -> 3
  | Ev_compare -> 4
  | Ev_line_change -> 5
  | Ev_region_done -> 6

let event_mask ev = 1 lsl event_bit ev

let event_of_name name =
  match String.lowercase_ascii name with
  | "ev_xfer_done" | "xfer_done" -> Some Ev_xfer_done
  | "ev_timer_done" | "timer_done" -> Some Ev_timer_done
  | "ev_pin_rise" | "pin_rise" -> Some Ev_pin_rise
  | "ev_pin_fall" | "pin_fall" -> Some Ev_pin_fall
  | "ev_compare" | "compare" -> Some Ev_compare
  | "ev_line_change" | "line_change" -> Some Ev_line_change
  | "ev_region_done" | "region_done" -> Some Ev_region_done
  | _ -> None

type action_op =
  | Act_nop
  | Act_gpio
  | Act_sample
  | Act_shift
  | Act_count
  | Act_crc
  | Act_next
  | Act_delay
  | Act_repeat
  | Act_done

let action_op_code = function
  | Act_nop -> 0
  | Act_gpio -> 1
  | Act_sample -> 2
  | Act_shift -> 3
  | Act_count -> 4
  | Act_crc -> 5
  | Act_next -> 6
  | Act_delay -> 7
  | Act_repeat -> 8
  | Act_done -> 9

let action_op_of_name name =
  match String.lowercase_ascii name with
  | "act_nop" | "nop" -> Some Act_nop
  | "act_gpio" | "gpio" -> Some Act_gpio
  | "act_sample" | "sample" -> Some Act_sample
  | "act_shift" | "shift" -> Some Act_shift
  | "act_count" | "count" -> Some Act_count
  | "act_crc" | "crc" -> Some Act_crc
  | "act_next" | "next" -> Some Act_next
  | "act_delay" | "delay" -> Some Act_delay
  | "act_repeat" | "repeat" -> Some Act_repeat
  | "act_done" | "done" -> Some Act_done
  | _ -> None

let action_word ~op ~args = ((op land 0xF) lsl 12) lor (args land 0xFFF)

let nop = 0x00
let halt = 0x01
let tx_load = 0x40
let rx_push = 0x70
let shift_clear = 0xA0
let event_stamp = 0xAF
let crc_finalize = 0xA3
let crc_push_lo = 0xA4
let crc_push_hi = 0xA5
let crc32_setup = 0xE1
let crc_push_b2 = 0xE2
let crc_push_b3 = 0xE3
let line_release = 0xA8
let line_sample = 0xA9
let wait_region = 0xE6

let check_range name lo hi n =
  if n < lo || n > hi then
    Error (Printf.sprintf "%s %d out of range [%d, %d]" name n lo hi)
  else Ok n

let check_reg = check_range "register" 0 (rf_count - 1)
let check_pin = check_range "pin" 0 7
let check_addr = check_range "address" 0 (sram_size - 1)
let check_u8 name = check_range name 0 0xFF
let check_u16 name = check_range name 0 0xFFFF

let le16 n = [ n land 0xFF; (n lsr 8) land 0xFF ]
let addr_bytes n = [ n land 0xFF; (n lsr 8) land 0x03 ]

let encode_wait16 cycles =
  if cycles < 0 || cycles > 0xFFFF then
    invalid_arg "WAIT16 duration must fit in 16 bits";
  0x10 :: le16 cycles

let encode_start_timer cycles =
  if cycles < 0 || cycles > 0xFFFF then
    invalid_arg "START_TIMER duration must fit in 16 bits";
  0xE0 :: le16 cycles

let encode_wait_event mask =
  if mask < 0 || mask > 0xFF then invalid_arg "WAIT_EVENT mask must fit in 8 bits";
  [ 0xD0; mask land 0xFF ]

let encode_arm_edges ~rise ~fall ~compare =
  [ 0xF0 lor (if compare then 1 else 0); rise land 0xFF; fall land 0xFF ]

let encode_gpio_write ~pin ~value =
  0x20 lor ((value land 1) lsl 3) lor (pin land 7)

let encode_gpio_oe ~pin ~enabled =
  0x30 lor ((enabled land 1) lsl 3) lor (pin land 7)

let encode_shift_out ~pin = 0x50 lor (pin land 7)
let encode_shift_in ~pin = 0x60 lor (pin land 7)

let encode_wait_pin ~pin ~value =
  0x90 lor ((value land 1) lsl 3) lor (pin land 7)

let encode_sideset ~pin ~value =
  (* programs.py:sideset — 0x02–0x0F prefixes; 0x00 is NOP, 0x01 is HALT. *)
  let imm = ((value land 1) lsl 3) lor (pin land 7) in
  if imm = 0 then [ nop ]
  else if imm = 1 then [ encode_gpio_write ~pin ~value ]
  else [ imm ]

let encode_jump addr = 0x80 :: addr_bytes addr
let encode_jz addr = 0x81 :: addr_bytes addr
let encode_jnz addr = 0x82 :: addr_bytes addr

let encode_djnz ~reg ~addr = (0x88 lor (reg land 7)) :: addr_bytes addr

let encode_set ~reg ~imm8 = [ 0xAA; reg land 7; imm8 land 0xFF ]

let encode_mov ~dst ~src = [ 0xAB; ((src land 7) lsl 3) lor (dst land 7) ]

let encode_alu ~op ~dst ~src =
  [ 0xAC; alu_code op land 7; ((src land 7) lsl 3) lor (dst land 7) ]

let encode_get_time ~reg = [ 0xAD; reg land 7 ]
let encode_wait_until ~reg = [ 0xAE; reg land 7 ]

let encode_map_pin ~logical ~physical =
  [ 0xB0 lor (logical land 7); physical land 7 ]

let encode_crc_setup ~width ~poly ~refin ~refout ~xor_ones ~init_ones =
  if width < 1 || width > 16 then invalid_arg "CRC width must be 1..16";
  let bit b n = if b then 1 lsl n else 0 in
  let cfg =
    ((width - 1) land 0xF)
    lor bit refin 4
    lor bit refout 5
    lor bit xor_ones 6
    lor bit init_ones 7
  in
  [ 0xA1; cfg; poly land 0xFF; (poly lsr 8) land 0xFF ]

let encode_crc_feed byte = [ 0xA2; byte land 0xFF ]

let encode_line_cfg ~pin_a ~pin_b ~jk_swap =
  let pins =
    (pin_a land 7)
    lor ((pin_b land 7) lsl 3)
    lor (if jk_swap then 1 lsl 6 else 0)
  in
  [ 0xA6; pins ]

let encode_line_drive state = [ 0xA7; state land 3 ]

let encode_start_xfer
    ~clk_pin ~tx_pin ~rx_pin ~bit_count ~half_period ~msb_first ~clk_idle
    ~sample_phase ~tx_open_drain ~clk_open_drain ~wait_clk_high =
  if bit_count < 1 || bit_count > 16 then
    invalid_arg "START_XFER bit_count must be 1..16";
  if half_period < 0 || half_period > 0xFF then
    invalid_arg "START_XFER half_period must fit in 8 bits";
  let cfg =
    ((bit_count - 1) land 0xF)
    lor ((if msb_first then 1 else 0) lsl 4)
    lor ((clk_idle land 1) lsl 5)
    lor ((sample_phase land 1) lsl 6)
    lor ((if tx_open_drain then 1 else 0) lsl 7)
  in
  let pins =
    (tx_pin land 7)
    lor ((rx_pin land 7) lsl 3)
    lor ((if clk_open_drain then 1 else 0) lsl 6)
    lor ((if wait_clk_high then 1 else 0) lsl 7)
  in
  [ 0xC0 lor (clk_pin land 7); cfg; pins; half_period land 0xFF ]

let encode_run_region slot = [ 0xE4; slot land 7 ]

let encode_run_region_n ~slot ~count =
  if count < 0 || count > 0xFF then
    invalid_arg "RUN_REGION_N count must fit in 8 bits";
  [ 0xE5; slot land 7; count land 0xFF ]

let encode_read_result rd = [ 0xE7; rd land 7 ]

let encode_action_wr_lo ~slot ~data = [ 0xE8; slot land 7; data land 0xFF ]
let encode_action_wr_hi ~slot ~data = [ 0xE9; slot land 7; data land 0xFF ]
let encode_action_load_shift data = [ 0xEA; data land 0xFF ]

let encode_prog_action ~slot ~word =
  encode_action_wr_lo ~slot ~data:word
  @ encode_action_wr_hi ~slot ~data:(word lsr 8)
