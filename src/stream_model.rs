//! Rate and storage bounds for periodic host-fed vector streams.
//!
//! This is an analytic feasibility model. It does not simulate FIFO phase,
//! controller instruction traffic, or physical memory timing.

/// Parameters for a byte-per-cell output stream with repeated logical rows.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct VectorStreamConfig {
    /// Output cells in each visible line.
    pub cells_per_line: u64,
    /// Number of visible scanlines.
    pub visible_lines: u64,
    /// Number of scanlines that repeat each logical input row.
    pub row_repeat: u64,
    /// Independent payload bits emitted simultaneously at each cell.
    pub independent_data_lanes: u64,
    /// Physical pads assigned to payload, including duplicated values.
    pub data_pads: u64,
    /// Physical pads assigned to sync or other sideband signals.
    pub sideband_pads: u64,
    /// Total available GPIO pads.
    pub total_pads: u64,
    /// Clock cycles between output cells.
    pub cell_cycles: u64,
    /// Non-active cycles per output line.
    pub horizontal_blank_cycles: u64,
    /// Total output lines, including vertical blanking.
    pub frame_lines: u64,
    /// Host clock cycles required to deliver one byte.
    pub host_cycles_per_byte: u64,
    /// Existing transmit FIFO capacity in bytes.
    pub fifo_depth_bytes: u64,
    /// Bytes per frame consumed by another host-fed workload.
    pub concurrent_host_bytes_per_frame: u64,
}

/// One storage choice evaluated for a vector stream.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct VectorStreamCandidate {
    /// Candidate name.
    pub name: &'static str,
    /// Host bytes required per frame by the primary stream.
    pub primary_host_bytes_per_frame: u64,
    /// Total host bytes required per frame, including competing traffic.
    pub host_bytes_per_frame: u64,
    /// Estimated local output reads per frame.
    pub local_reads_per_frame: u64,
    /// Host writes into the local row store per frame.
    pub local_writes_per_frame: u64,
    /// Storage bytes required by this candidate, including the existing TX FIFO.
    pub storage_bytes: u64,
    /// Host link utilization as an integer percentage, rounded down.
    pub host_utilization_percent: u64,
    /// Local one-read-per-cycle port utilization as an integer percentage.
    pub local_read_port_percent: u64,
    /// Primary-stream host slack, excluding competing traffic and switches.
    pub refill_slack_cycles: i128,
}

/// Three rate/storage candidates for periodic row-repeated vector output.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct VectorStreamModel {
    /// Total cycles in a frame.
    pub frame_cycles: u64,
    /// Output bytes consumed in a frame, across all visible scanlines.
    pub output_bytes_per_frame: u64,
    /// Independent payload bits delivered over the visible frame.
    pub useful_data_bits_per_frame: u64,
    /// Physical pads used by payload and sideband signals.
    pub pads_used: u64,
    /// Total available GPIO pads.
    pub total_pads: u64,
    /// Maximum direct-stream FIFO underflows in the swept host phases.
    pub direct_fifo_worst_underflows: u64,
    /// Number of swept phases that complete without an output underflow.
    pub direct_fifo_passing_phases: u64,
    /// Number of host phases checked; capped at 256 for large service periods.
    pub direct_fifo_phases_swept: u64,
    /// Direct stream from the host into the transmit FIFO.
    pub direct_fifo: VectorStreamCandidate,
    /// One row store, refilled only after the current row's last output use.
    pub single_row: VectorStreamCandidate,
    /// Two local rows, allowing one row to be filled while the other is read.
    pub ping_pong: VectorStreamCandidate,
}

impl VectorStreamModel {
    /// Evaluate direct streaming, a single row buffer, and ping-pong buffers.
    ///
    /// Returns an error for zero parameters, non-divisible row repetition, or
    /// arithmetic overflow. The model assumes one local read per output byte
    /// and one local port operation per cycle. A single row can be refilled
    /// only during horizontal blanking; ping-pong storage can refill the
    /// inactive row while the active row repeats.
    pub fn evaluate(config: VectorStreamConfig) -> Result<Self, &'static str> {
        let VectorStreamConfig {
            cells_per_line,
            visible_lines,
            row_repeat,
            independent_data_lanes,
            data_pads,
            sideband_pads,
            total_pads,
            cell_cycles,
            horizontal_blank_cycles,
            frame_lines,
            host_cycles_per_byte,
            fifo_depth_bytes,
            concurrent_host_bytes_per_frame,
        } = config;
        if [
            cells_per_line,
            visible_lines,
            row_repeat,
            independent_data_lanes,
            total_pads,
            cell_cycles,
            frame_lines,
            host_cycles_per_byte,
            fifo_depth_bytes,
        ]
        .contains(&0)
        {
            return Err("all parameters except horizontal blanking must be nonzero");
        }
        if data_pads < independent_data_lanes {
            return Err("payload pads must cover all independent data lanes");
        }
        let pads_used = data_pads
            .checked_add(sideband_pads)
            .ok_or("GPIO pad count overflows")?;
        if pads_used > total_pads {
            return Err("payload and sideband pads exceed available GPIO pads");
        }
        if visible_lines % row_repeat != 0 {
            return Err("visible lines must be divisible by row repetition");
        }
        if visible_lines > frame_lines {
            return Err("visible lines cannot exceed total frame lines");
        }
        let line_cycles = cells_per_line
            .checked_mul(cell_cycles)
            .and_then(|active| active.checked_add(horizontal_blank_cycles))
            .ok_or("line-cycle count overflows")?;
        let frame_cycles = frame_lines
            .checked_mul(line_cycles)
            .ok_or("frame-cycle count overflows")?;
        let output_bytes_per_frame = cells_per_line
            .checked_mul(visible_lines)
            .ok_or("output-byte count overflows")?;
        let useful_data_bits_per_frame = output_bytes_per_frame
            .checked_mul(independent_data_lanes)
            .ok_or("useful data bit count overflows")?;
        let (direct_fifo_worst_underflows, direct_fifo_passing_phases, direct_fifo_phases_swept) =
            simulate_direct_fifo_phases(
                cells_per_line,
                visible_lines,
                cell_cycles,
                line_cycles,
                host_cycles_per_byte,
                fifo_depth_bytes,
                output_bytes_per_frame,
            )?;
        let row_count = visible_lines / row_repeat;
        let row_bytes = cells_per_line
            .checked_mul(row_count)
            .ok_or("row-buffer host-byte count overflows")?;
        let local_reads = output_bytes_per_frame;
        let host_cycles = |bytes: u64| {
            bytes
                .checked_mul(host_cycles_per_byte)
                .ok_or("host service-cycle count overflows")
        };
        let direct_total_host_bytes = output_bytes_per_frame
            .checked_add(concurrent_host_bytes_per_frame)
            .ok_or("total direct host-byte count overflows")?;
        let row_total_host_bytes = row_bytes
            .checked_add(concurrent_host_bytes_per_frame)
            .ok_or("total row-buffer host-byte count overflows")?;
        let direct_host_cycles = host_cycles(direct_total_host_bytes)?;
        let row_host_cycles = host_cycles(row_total_host_bytes)?;
        let percent = |cycles: u64| cycles.saturating_mul(100) / frame_cycles;
        let direct_line_slack = i128::from(line_cycles) - i128::from(host_cycles(cells_per_line)?);
        let single_row_refill_slack =
            i128::from(horizontal_blank_cycles) - i128::from(host_cycles(cells_per_line)?);
        let row_refill_window = line_cycles
            .checked_mul(row_repeat)
            .ok_or("row refill window overflows")?;
        let row_refill_slack =
            i128::from(row_refill_window) - i128::from(host_cycles(cells_per_line)?);
        let candidate = |name,
                         primary_bytes,
                         host_bytes,
                         host_service_cycles,
                         storage_bytes,
                         local_reads,
                         local_writes,
                         slack| {
            VectorStreamCandidate {
                name,
                primary_host_bytes_per_frame: primary_bytes,
                host_bytes_per_frame: host_bytes,
                local_reads_per_frame: local_reads,
                local_writes_per_frame: local_writes,
                storage_bytes,
                host_utilization_percent: percent(host_service_cycles),
                local_read_port_percent: percent(local_reads),
                refill_slack_cycles: slack,
            }
        };
        Ok(Self {
            frame_cycles,
            output_bytes_per_frame,
            useful_data_bits_per_frame,
            pads_used,
            total_pads,
            direct_fifo_worst_underflows,
            direct_fifo_passing_phases,
            direct_fifo_phases_swept,
            direct_fifo: candidate(
                "direct_fifo",
                output_bytes_per_frame,
                direct_total_host_bytes,
                direct_host_cycles,
                fifo_depth_bytes,
                0,
                0,
                direct_line_slack,
            ),
            single_row: candidate(
                "single_row",
                row_bytes,
                row_total_host_bytes,
                row_host_cycles,
                cells_per_line
                    .checked_add(fifo_depth_bytes)
                    .ok_or("single-row storage overflows")?,
                local_reads,
                row_bytes,
                single_row_refill_slack,
            ),
            ping_pong: candidate(
                "ping_pong",
                row_bytes,
                row_total_host_bytes,
                row_host_cycles,
                cells_per_line
                    .checked_mul(2)
                    .and_then(|bytes| bytes.checked_add(fifo_depth_bytes))
                    .ok_or("ping-pong storage overflows")?,
                local_reads,
                row_bytes,
                row_refill_slack,
            ),
        })
    }
}

/// Sweep host-write phase against active pixel reads using a finite FIFO.
///
/// Host attempts are periodic and blocked attempts are retried one service
/// interval later. The FIFO is preloaded to capacity, matching the VGA RTL
/// test. Output wins same-cycle ties, giving a conservative phase result.
/// At most 256 evenly spaced phases are sampled for large service periods.
fn simulate_direct_fifo_phases(
    cells_per_line: u64,
    visible_lines: u64,
    cell_cycles: u64,
    line_cycles: u64,
    host_cycles_per_byte: u64,
    fifo_depth_bytes: u64,
    output_bytes_per_frame: u64,
) -> Result<(u64, u64, u64), &'static str> {
    const MAX_PHASES: u64 = 256;
    let phase_count = host_cycles_per_byte.min(MAX_PHASES);
    let initial_fill = fifo_depth_bytes.min(output_bytes_per_frame);
    let mut worst_underflows = 0;
    let mut passing_phases = 0;
    for phase_index in 0..phase_count {
        let phase = phase_index
            .checked_mul(host_cycles_per_byte)
            .ok_or("host phase overflows")?
            / phase_count;
        let mut fifo = initial_fill;
        let mut host_bytes_remaining = output_bytes_per_frame - initial_fill;
        let mut next_host_attempt = phase;
        let mut underflows = 0;
        for line in 0..visible_lines {
            let line_start = line
                .checked_mul(line_cycles)
                .ok_or("line start overflows")?;
            for cell in 0..cells_per_line {
                let output_cycle = cell
                    .checked_mul(cell_cycles)
                    .and_then(|offset| line_start.checked_add(offset))
                    .ok_or("output cycle overflows")?;
                while next_host_attempt < output_cycle {
                    if host_bytes_remaining > 0 && fifo < fifo_depth_bytes {
                        fifo += 1;
                        host_bytes_remaining -= 1;
                    }
                    next_host_attempt = next_host_attempt
                        .checked_add(host_cycles_per_byte)
                        .ok_or("host attempt cycle overflows")?;
                }
                if fifo > 0 {
                    fifo -= 1;
                } else {
                    underflows += 1;
                }
                if next_host_attempt == output_cycle {
                    if host_bytes_remaining > 0 && fifo < fifo_depth_bytes {
                        fifo += 1;
                        host_bytes_remaining -= 1;
                    }
                    next_host_attempt = next_host_attempt
                        .checked_add(host_cycles_per_byte)
                        .ok_or("host attempt cycle overflows")?;
                }
            }
        }
        worst_underflows = worst_underflows.max(underflows);
        if underflows == 0 {
            passing_phases += 1;
        }
    }
    Ok((worst_underflows, passing_phases, phase_count))
}

#[cfg(test)]
mod tests {
    use super::{VectorStreamConfig, VectorStreamModel};

    fn vga_config() -> VectorStreamConfig {
        VectorStreamConfig {
            cells_per_line: 80,
            visible_lines: 480,
            row_repeat: 8,
            independent_data_lanes: 6,
            data_pads: 6,
            sideband_pads: 2,
            total_pads: 8,
            cell_cycles: 16,
            horizontal_blank_cycles: 320,
            frame_lines: 525,
            host_cycles_per_byte: 10,
            fifo_depth_bytes: 8,
            concurrent_host_bytes_per_frame: 0,
        }
    }

    #[test]
    fn vga_tradeoff_matches_frame_rate_bounds() {
        let model = VectorStreamModel::evaluate(vga_config()).unwrap();
        assert_eq!(model.frame_cycles, 840_000);
        assert_eq!(model.output_bytes_per_frame, 38_400);
        assert_eq!(model.useful_data_bits_per_frame, 230_400);
        assert_eq!(model.pads_used, 8);
        assert_eq!(model.total_pads, 8);
        assert_eq!(model.direct_fifo_worst_underflows, 0);
        assert_eq!(model.direct_fifo_passing_phases, 10);
        assert_eq!(model.direct_fifo_phases_swept, 10);
        assert_eq!(model.direct_fifo.host_bytes_per_frame, 38_400);
        assert_eq!(model.direct_fifo.host_utilization_percent, 45);
        assert_eq!(model.direct_fifo.refill_slack_cycles, 800);
        assert_eq!(model.single_row.host_bytes_per_frame, 4_800);
        assert_eq!(model.single_row.storage_bytes, 88);
        assert_eq!(model.single_row.refill_slack_cycles, -480);
        assert_eq!(model.single_row.local_reads_per_frame, 38_400);
        assert_eq!(model.single_row.local_writes_per_frame, 4_800);
        assert_eq!(model.single_row.local_read_port_percent, 4);
        assert_eq!(model.ping_pong.storage_bytes, 168);
    }

    #[test]
    fn rejects_rows_that_do_not_repeat_evenly() {
        let mut config = vga_config();
        config.row_repeat = 7;
        assert_eq!(
            VectorStreamModel::evaluate(config),
            Err("visible lines must be divisible by row repetition")
        );
    }

    #[test]
    fn one_replicated_data_bit_uses_same_pads_but_carries_six_times_less_data() {
        let mut config = vga_config();
        config.independent_data_lanes = 1;
        let model = VectorStreamModel::evaluate(config).unwrap();
        assert_eq!(model.pads_used, 8);
        assert_eq!(model.useful_data_bits_per_frame, 38_400);
        assert_eq!(
            6,
            VectorStreamModel::evaluate(vga_config())
                .unwrap()
                .useful_data_bits_per_frame
                / model.useful_data_bits_per_frame
        );
    }

    #[test]
    fn rejects_vector_pad_overallocation() {
        let mut config = vga_config();
        config.sideband_pads = 3;
        assert_eq!(
            VectorStreamModel::evaluate(config),
            Err("payload and sideband pads exceed available GPIO pads")
        );
    }

    #[test]
    fn row_storage_can_fit_host_contention_that_direct_stream_cannot() {
        let mut config = vga_config();
        config.host_cycles_per_byte = 15;
        config.concurrent_host_bytes_per_frame = 42_000;
        let model = VectorStreamModel::evaluate(config).unwrap();
        assert_eq!(model.direct_fifo.primary_host_bytes_per_frame, 38_400);
        assert_eq!(model.direct_fifo.host_bytes_per_frame, 80_400);
        assert_eq!(model.single_row.primary_host_bytes_per_frame, 4_800);
        assert_eq!(model.single_row.host_bytes_per_frame, 46_800);
        assert_eq!(model.direct_fifo.host_utilization_percent, 143);
        assert_eq!(model.single_row.host_utilization_percent, 83);
        assert_eq!(model.single_row.refill_slack_cycles, -880);
        assert_eq!(model.ping_pong.refill_slack_cycles, 11_600);
    }

    #[test]
    fn direct_fifo_phase_sweep_catches_active_burst_starvation() {
        let mut config = vga_config();
        config.host_cycles_per_byte = 20;
        let model = VectorStreamModel::evaluate(config).unwrap();
        assert!(model.direct_fifo_worst_underflows > 0);
        assert!(model.direct_fifo_passing_phases < model.direct_fifo_phases_swept);
    }
}
