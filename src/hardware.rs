//! What the chip has, and where each register lives.
//!
//! Every number here comes from the spatial RTL at [`crate::HARDWARE_REVISION`]:
//! `src/protocol_emulator_core.v` for the signal space and configuration map,
//! and the per-tile `src/fabric_*.v` headers for register layouts.

/// Physical `uio` pads available to programs.
pub(crate) const PADS: usize = 8;
pub(crate) const TIMERS: usize = 4;
pub(crate) const SHIFTERS: usize = 2;
pub(crate) const COUNTERS: usize = 2;
pub(crate) const LOGIC_CELLS: usize = 8;
pub(crate) const EDGE_CELLS: usize = 8;
/// Controller `FIRE` pulse lines.
pub(crate) const EVENTS: usize = 2;
/// Controller flag levels.
pub(crate) const FLAGS: usize = 2;
pub(crate) const CRCS: usize = 2;
/// Widest CRC register.
pub(crate) const MAX_CRC_BITS: i128 = 32;

/// Source times are measured at the pins: a timer's `first` is the time
/// from its start to its first tick's effect as seen at the pins. On the
/// two paths whose both ends are pins, this chip adds a latency that the
/// compiler subtracts (`Chip::timer_pin_latency`); every other timer is
/// internal at one end, so its counts are taken as written.
///
/// An input pin's edge starts the timer and its tick samples a pin: the
/// sample lands this many cycles after the count (the pin synchronizer).
pub(crate) const PIN_EDGE_TO_SAMPLE: usize = 1;
/// A shifter's `busy` starts the timer and its tick shifts that shifter: the
/// first bit at the pin lasts this many cycles longer than the count (the
/// start pulse and the pin register).
pub(crate) const SHIFT_START_TO_SHIFT: usize = 2;
pub(crate) const BYTE_STAGES: usize = 2;
pub(crate) const BYTE_TILES: usize = 8;
pub(crate) const SRAM_BYTES: usize = 1024;
/// Shifter word length range.
pub(crate) const MAX_WORD_BITS: i128 = 16;
/// Words wider than this cannot be moved through an 8-bit FIFO automatically.
pub(crate) const FIFO_WORD_BITS: i128 = 8;
/// Framing bits before or after the data bits.
pub(crate) const MAX_FRAME_BITS: i128 = 3;
/// Timer reload and phase registers.
pub(crate) const MAX_TIMER_CYCLES: i128 = 1 << 16;

/// One of the 64 signal-space lines. Lines 0–31 are one-cycle pulses
/// (with the constants 0 and 1), lines 32–63 are levels.
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub(crate) struct Line(pub u8);

impl Line {
    pub(crate) const ZERO: Line = Line(0);
    pub(crate) const ONE: Line = Line(1);

    pub(crate) fn is_level(self) -> bool {
        self.0 >= 32
    }

    pub(crate) fn is_constant(self) -> bool {
        self.0 < 2
    }

    /// The 5-bit encoding used by level-only selectors.
    pub(crate) fn level_select(self) -> u16 {
        debug_assert!(self.is_level());
        u16::from(self.0 - 32)
    }

    pub(crate) fn fire(k: usize) -> Line {
        Line(2 + k as u8)
    }
    pub(crate) fn timer_rise(k: usize) -> Line {
        Line(4 + 2 * k as u8)
    }
    pub(crate) fn timer_fall(k: usize) -> Line {
        Line(5 + 2 * k as u8)
    }
    pub(crate) fn shifter_word(k: usize) -> Line {
        Line(12 + 2 * k as u8)
    }
    pub(crate) fn shifter_stop(k: usize) -> Line {
        Line(13 + 2 * k as u8)
    }
    pub(crate) fn edge(k: usize) -> Line {
        Line(16 + k as u8)
    }
    pub(crate) fn pin_level(p: usize) -> Line {
        Line(32 + p as u8)
    }
    pub(crate) fn timer_out(k: usize) -> Line {
        Line(40 + k as u8)
    }
    pub(crate) fn shifter_out(k: usize) -> Line {
        Line(44 + k as u8)
    }
    pub(crate) fn shifter_active(k: usize) -> Line {
        Line(46 + k as u8)
    }
    pub(crate) fn counter_at_least(k: usize) -> Line {
        Line(48 + k as u8)
    }
    pub(crate) fn counter_reached(k: usize) -> Line {
        Line(24 + k as u8)
    }
    pub(crate) fn crc_out(k: usize) -> Line {
        Line(if k == 0 { 52 } else { 50 })
    }
    pub(crate) fn crc_ok(k: usize) -> Line {
        Line(if k == 0 { 53 } else { 51 })
    }
    pub(crate) fn flag(k: usize) -> Line {
        Line(54 + k as u8)
    }
    pub(crate) fn logic(k: usize) -> Line {
        Line(56 + k as u8)
    }
}

/// Configuration addresses written by the controller's `WCF`/`CFG`.
pub(crate) mod config {
    pub(crate) fn timer(k: usize, register: u8) -> u8 {
        4 * k as u8 + register
    }
    pub(crate) fn shifter(k: usize, register: u8) -> u8 {
        0x10 + 4 * k as u8 + register
    }
    pub(crate) fn shifter_burst(k: usize) -> u8 {
        0x32 + k as u8
    }
    pub(crate) fn shifter_coder(k: usize) -> u8 {
        0x36 + k as u8
    }
    pub(crate) fn shifter_coder_gate(k: usize) -> u8 {
        0x38 + k as u8
    }
    pub(crate) fn pin(p: usize) -> u8 {
        0x18 + p as u8
    }
    pub(crate) fn counter(k: usize, register: u8) -> u8 {
        0x20 + 4 * k as u8 + register
    }
    pub(crate) fn logic(k: usize, register: u8) -> u8 {
        0x40 + 4 * k as u8 + register
    }
    pub(crate) fn byte_tile(tile: usize, register: u8) -> u8 {
        0x60 + 4 * tile as u8 + register
    }
    pub(crate) fn crc(k: usize, register: u8) -> u8 {
        (if k == 0 { 0x28 } else { 0x88 }) + register
    }
    pub(crate) fn edge(k: usize) -> u8 {
        0x80 + k as u8
    }
    /// Controller flags, one bit each.
    pub(crate) const FLAGS: u8 = 0x30;
    /// Global context-select line (low six bits) and enable (bit six).
    pub(crate) const CONTEXT_SELECT: u8 = 0x34;
    /// Configuration-write mask: bit zero for context 0, bit one for context 1.
    pub(crate) const CONTEXT_WRITE: u8 = 0x35;
    /// Writing 1 goes live; tiles and pins stay idle until then.
    pub(crate) const ARM: u8 = 0x31;
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub(crate) enum PinMode {
    Input = 0,
    PushPull = 1,
    OpenDrain = 2,
}
