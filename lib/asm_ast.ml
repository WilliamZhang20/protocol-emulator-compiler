(** Assembler AST. Mnemonics match protocol-emulator [programs.py] / architecture.md. *)

type reg = int
type pin = int

type operand =
  | Imm of int
  | Reg of reg
  | Label of string
  | Event of Isa.event
  | Action_op of Isa.action_op

type xfer = {
  clk_pin : operand;
  tx_pin : operand;
  rx_pin : operand;
  bit_count : operand;
  half_period : operand;
  msb_first : bool;
  clk_idle : int;
  sample_phase : int;
  tx_open_drain : bool;
  clk_open_drain : bool;
  wait_clk_high : bool;
}

type crc_setup = {
  width : operand;
  poly : operand;
  refin : bool;
  refout : bool;
  xor_ones : bool;
  init_ones : bool;
}

type mnemonic =
  | Nop
  | Halt
  | Wait16 of operand
  | Start_timer of operand
  | Wait_event of operand
  | Arm_edges of { rise : operand; fall : operand; compare : bool }
  | Gpio_write of { pin : operand; value : operand }
  | Gpio_oe of { pin : operand; enabled : operand }
  | Tx_load
  | Rx_push
  | Shift_out of operand
  | Shift_in of operand
  | Shift_clear
  | Jmp of operand
  | Jz of operand
  | Jnz of operand
  | Djnz of { reg : operand; target : operand }
  | Set of { reg : operand; imm : operand }
  | Mov of { dst : operand; src : operand }
  | Alu of { op : Isa.alu_op; dst : operand; src : operand }
  | Get_time of operand
  | Wait_until of operand
  | Event_stamp
  | Wait_pin of { pin : operand; value : operand }
  | Sideset of { pin : operand; value : operand }
  | Map_pin of { logical : operand; physical : operand }
  | Crc_setup of crc_setup
  | Crc_feed of operand
  | Crc_finalize
  | Crc_push_lo
  | Crc_push_hi
  | Crc32_setup
  | Crc_push_b2
  | Crc_push_b3
  | Line_cfg of { pin_a : operand; pin_b : operand; jk_swap : bool }
  | Line_drive of operand
  | Line_release
  | Line_sample
  | Start_xfer of xfer
  | Run_region of operand
  | Run_region_n of { slot : operand; count : operand }
  | Wait_region
  | Read_result of operand
  | Action_wr_lo of { slot : operand; data : operand }
  | Action_wr_hi of { slot : operand; data : operand }
  | Action_load_shift of operand
  | Prog_action of { slot : operand; word : operand }
  | Action_word of { op : operand; args : operand }
    (** Encode a 16-bit action word as two immediates via [prog_action] is preferred.
        This form is a placeholder that still needs a destination slot — see [Assemble]. *)

type stmt =
  | Label_def of { loc : Loc.t; name : string }
  | Instr of { loc : Loc.t; op : mnemonic }

type program = { file : string; stmts : stmt list }
