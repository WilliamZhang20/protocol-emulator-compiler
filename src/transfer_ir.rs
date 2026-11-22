//! Typed stored-stream operations and their PX8 scheduling contract.
//!
//! The logical transfers are distinct from their interleaved physical SRAM
//! table. Scheduling preserves each stream's byte order and tags its final byte.
use serde::Serialize;

/// An allocated contiguous object within the shared program SRAM.
#[derive(Clone, Debug, Serialize)]
pub struct MemoryObject {
    /// `configuration` or `constant_data`.
    pub kind: &'static str,
    /// Inclusive byte address within the SRAM image.
    pub start: usize,
    /// Size in bytes.
    pub length: usize,
    /// Number of generated pointer references to this object.
    pub references: usize,
}

/// Exact layout selected by the current PX8 backend.
#[derive(Clone, Debug, Serialize)]
pub struct ProgramMemoryLayout {
    /// Capacity of the target's shared program SRAM in bytes.
    pub capacity: usize,
    /// Number of instruction bytes beginning at address zero.
    pub code_bytes: usize,
    /// Total image length, including instructions and all objects.
    pub image_bytes: usize,
    /// Data objects, ordered by address. Configuration and tables can interleave.
    pub objects: Vec<MemoryObject>,
    /// Compile-time stored transfers and the chosen SRAM tables and schedules.
    pub stored_transfers: Vec<StoredTransfer>,
}

/// One compile-time byte stream sent to a physical shifter FIFO.
#[derive(Clone, Debug, Serialize)]
pub struct StoredStream {
    /// Shifter and TX FIFO index.
    pub channel: usize,
    /// Payload bytes in transmission order.
    pub bytes: Vec<u8>,
}

/// A repeated sequence of SRAM reads and TX FIFO writes.
#[derive(Clone, Debug, Serialize)]
pub struct TransferPhase {
    /// Number of repetitions of this sequence.
    pub repetitions: usize,
    /// Each `(channel, end_of_packet)` pair consumes the next table byte.
    pub writes: Vec<(usize, bool)>,
}

/// A stored-transfer operation after schedule and table selection.
#[derive(Clone, Debug, Serialize)]
pub struct StoredTransfer {
    /// Logical source streams; two streams are requested in parallel.
    pub streams: Vec<StoredStream>,
    /// SRAM bytes in the exact order consumed by the selected schedule.
    pub table: Vec<u8>,
    /// PX8 schedule, in execution order.
    pub phases: Vec<TransferPhase>,
    /// Byte address of the selected table in the assembled image.
    pub table_address: usize,
}

impl StoredTransfer {
    /// Form a schedule for one or two nonempty, distinct FIFO streams.
    ///
    /// This schedule interleaves both streams while both have data and drains
    /// any remainder. FIFO backpressure can stall either stream; no independent
    /// progress or protocol deadline is guaranteed by this schedule.
    pub fn schedule(streams: Vec<StoredStream>) -> Result<Self, &'static str> {
        if streams.is_empty() || streams.len() > 2 || streams.iter().any(|s| s.bytes.is_empty()) {
            return Err("stored transfer needs one or two nonempty streams");
        }
        if streams.len() == 2 && streams[0].channel == streams[1].channel {
            return Err("stored transfer channels must be distinct");
        }
        if streams
            .iter()
            .any(|s| s.channel >= crate::hardware::SHIFTERS)
        {
            return Err("stored transfer channel is unavailable");
        }
        let common = streams.iter().map(|s| s.bytes.len()).min().unwrap();
        let mut table = Vec::new();
        for i in 0..common {
            for stream in &streams {
                table.push(stream.bytes[i]);
            }
        }
        for stream in &streams {
            table.extend_from_slice(&stream.bytes[common..]);
        }
        let mut phases = vec![
            TransferPhase {
                repetitions: common - 1,
                writes: streams.iter().map(|s| (s.channel, false)).collect(),
            },
            TransferPhase {
                repetitions: 1,
                writes: streams
                    .iter()
                    .map(|s| (s.channel, s.bytes.len() == common))
                    .collect(),
            },
        ];
        for stream in &streams {
            let remaining = stream.bytes.len() - common;
            if remaining > 0 {
                phases.push(TransferPhase {
                    repetitions: remaining - 1,
                    writes: vec![(stream.channel, false)],
                });
                phases.push(TransferPhase {
                    repetitions: 1,
                    writes: vec![(stream.channel, true)],
                });
            }
        }
        Ok(Self {
            streams,
            table,
            phases,
            table_address: 0,
        })
    }
}
