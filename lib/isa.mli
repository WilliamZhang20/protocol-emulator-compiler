(** protocol-emulator SRAM bytecode ISA.

    Encodings follow
    {{:https://github.com/WilliamZhang20/protocol-emulator/blob/main/docs/architecture.md}
    docs/architecture.md} and the Python assembler helpers in
    [test/cocotb_tests/reference/programs.py]. Do not invent opcodes here. *)

val sram_size : int
(** Foundry program memory is 1024 × 8. *)

val rf_count : int
(** 8 × 16 register file (R0–R7). *)

val rf_width_bits : int

val action_slots : int
(** Phase C action engine: 8 slots. *)

val addr_bits : int
(** Jump targets are 10-bit SRAM addresses. *)

type alu_op = Add | Sub | And | Or | Xor | Shl | Shr

val alu_code : alu_op -> int

type event =
  | Ev_xfer_done
  | Ev_timer_done
  | Ev_pin_rise
  | Ev_pin_fall
  | Ev_compare
  | Ev_line_change
  | Ev_region_done

val event_bit : event -> int
val event_mask : event -> int
val event_of_name : string -> event option

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

val action_op_code : action_op -> int
val action_op_of_name : string -> action_op option
val action_word : op:int -> args:int -> int

val nop : int
val halt : int
val tx_load : int
val rx_push : int
val shift_clear : int
val event_stamp : int
val crc_finalize : int
val crc_push_lo : int
val crc_push_hi : int
val crc32_setup : int
val crc_push_b2 : int
val crc_push_b3 : int
val line_release : int
val line_sample : int
val wait_region : int

val encode_wait16 : int -> int list
val encode_start_timer : int -> int list
val encode_wait_event : int -> int list
val encode_arm_edges : rise:int -> fall:int -> compare:bool -> int list
val encode_gpio_write : pin:int -> value:int -> int
val encode_gpio_oe : pin:int -> enabled:int -> int
val encode_shift_out : pin:int -> int
val encode_shift_in : pin:int -> int
val encode_wait_pin : pin:int -> value:int -> int
val encode_sideset : pin:int -> value:int -> int list
val encode_jump : int -> int list
val encode_jz : int -> int list
val encode_jnz : int -> int list
val encode_djnz : reg:int -> addr:int -> int list
val encode_set : reg:int -> imm8:int -> int list
val encode_mov : dst:int -> src:int -> int list
val encode_alu : op:alu_op -> dst:int -> src:int -> int list
val encode_get_time : reg:int -> int list
val encode_wait_until : reg:int -> int list
val encode_map_pin : logical:int -> physical:int -> int list
val encode_crc_setup :
  width:int ->
  poly:int ->
  refin:bool ->
  refout:bool ->
  xor_ones:bool ->
  init_ones:bool ->
  int list
val encode_crc_feed : int -> int list
val encode_line_cfg : pin_a:int -> pin_b:int -> jk_swap:bool -> int list
val encode_line_drive : int -> int list
val encode_start_xfer :
  clk_pin:int ->
  tx_pin:int ->
  rx_pin:int ->
  bit_count:int ->
  half_period:int ->
  msb_first:bool ->
  clk_idle:int ->
  sample_phase:int ->
  tx_open_drain:bool ->
  clk_open_drain:bool ->
  wait_clk_high:bool ->
  int list
val encode_run_region : int -> int list
val encode_run_region_n : slot:int -> count:int -> int list
val encode_read_result : int -> int list
val encode_action_wr_lo : slot:int -> data:int -> int list
val encode_action_wr_hi : slot:int -> data:int -> int list
val encode_action_load_shift : int -> int list
val encode_prog_action : slot:int -> word:int -> int list

val check_reg : int -> (int, string) result
val check_pin : int -> (int, string) result
val check_addr : int -> (int, string) result
val check_u8 : string -> int -> (int, string) result
val check_u16 : string -> int -> (int, string) result
