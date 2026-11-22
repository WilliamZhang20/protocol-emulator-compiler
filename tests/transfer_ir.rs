//! Stored transfer IR, SRAM layout, and exact schedule behavior.
use spatial::{StoredStream, StoredTransfer, compile};

#[test]
fn schedules_preserve_bytes_and_exactly_one_terminal_tag_per_stream() {
    for a_len in 1..=7 {
        for b_len in 1..=7 {
            let a: Vec<_> = (0..a_len).map(|i| i as u8).collect();
            let b: Vec<_> = (0..b_len).map(|i| 100 + i as u8).collect();
            let ir = StoredTransfer::schedule(vec![
                StoredStream {
                    channel: 0,
                    bytes: a.clone(),
                },
                StoredStream {
                    channel: 1,
                    bytes: b.clone(),
                },
            ])
            .unwrap();
            let mut read = 0;
            let mut received = [Vec::new(), Vec::new()];
            let mut ends = [Vec::new(), Vec::new()];
            for phase in &ir.phases {
                for _ in 0..phase.repetitions {
                    for &(channel, eop) in &phase.writes {
                        received[channel].push(ir.table[read]);
                        ends[channel].push(eop);
                        read += 1;
                    }
                }
            }
            assert_eq!(read, ir.table.len());
            assert_eq!(received, [a, b]);
            for flags in ends {
                assert_eq!(flags.iter().filter(|&&flag| flag).count(), 1);
                assert_eq!(flags.last(), Some(&true));
            }
        }
    }
}

#[test]
fn layout_tracks_actual_image_and_interned_data() {
    let build = compile("shifter s {} const D = [13,149,77,212] export fn a() { s.send(D) } export fn b() { s.send(D) }").unwrap();
    let layout = &build.memory_layout;
    assert_eq!(layout.image_bytes, build.image.len());
    let mut next = layout.code_bytes;
    for object in &layout.objects {
        assert_eq!(object.start, next);
        next += object.length;
    }
    assert_eq!(next, build.image.len());
    let tables: Vec<_> = layout
        .objects
        .iter()
        .filter(|o| o.kind == "constant_data")
        .collect();
    assert_eq!(tables.len(), 1);
    assert_eq!(tables[0].references, 2);
    assert_eq!(
        &build.image[tables[0].start..tables[0].start + tables[0].length],
        &[13, 149, 77, 212]
    );
    assert_eq!(layout.stored_transfers.len(), 2);
    assert!(
        layout
            .stored_transfers
            .iter()
            .all(|t| t.table_address == tables[0].start)
    );
}
