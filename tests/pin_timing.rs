//! Source times are measured at the pins: a timer's `first` runs from its
//! start to its first tick's effect as seen at the pins, and the compiler
//! subtracts what the chip adds on paths with a pin at both ends (an input
//! edge to a pin sample: 1 cycle; a shifter's start to its first shift at
//! the pin: 2). Each class is pinned by its boundary: one cycle more than
//! the latency compiles, the latency itself is refused; a timer with an
//! internal start takes its count as written.
use spatial::compile;

fn receiver(first: &str) -> String {
    format!(
        "clock 50 MHz pin rx: in
         timer bit {{ every: 64 cycles, first: {first}, pulse, run: rx.fall until r.done }}
         shifter r {{ frame: [0, data(8), 1], start: rx.fall, shift: bit.tick, sample: rx at bit.tick }}
         export stream recv: r -> host"
    )
}

fn transmitter(first: &str) -> String {
    format!(
        "clock 50 MHz pin tx: out = s.out
         timer bit {{ every: 64 cycles, first: {first}, pulse, run: s.busy until s.done }}
         shifter s {{ frame: [0, data(8), 1], idle: 1, shift: bit.tick }}
         export stream send: host -> s"
    )
}

fn refused(source: &str) -> bool {
    match compile(source) {
        Ok(_) => false,
        Err(e) => {
            assert!(format!("{e:?}").contains("measured at the pins"), "{e:?}");
            true
        }
    }
}

#[test]
fn input_edge_to_sample_subtracts_one_cycle() {
    assert!(refused(&receiver("1 cycle")));
    compile(&receiver("2 cycles")).unwrap();
    compile(&receiver("32 cycles")).unwrap();
}

#[test]
fn shifter_start_to_first_shift_subtracts_two_cycles() {
    assert!(refused(&transmitter("2 cycles")));
    compile(&transmitter("3 cycles")).unwrap();
    compile(&transmitter("64 cycles")).unwrap();
}

#[test]
fn internal_start_takes_the_count_as_written() {
    compile(
        "clock 50 MHz event go pin p: out = t.out
         timer t { every: 4 cycles, first: 1 cycle, idle: 0, run: go until t.fall }
         export fn f() { fire go }",
    )
    .unwrap();
}

#[test]
fn one_pin_cycle_moves_the_encoded_count_by_one() {
    // Different pin times give different images; equal ones the same.
    let a = compile(&receiver("32 cycles")).unwrap().image;
    let b = compile(&receiver("33 cycles")).unwrap().image;
    assert_ne!(a, b);
    assert_eq!(a, compile(&receiver("32 cycles")).unwrap().image);
}
