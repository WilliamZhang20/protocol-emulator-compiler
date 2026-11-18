//! Compile Spatial programs (`.spt`) for the PX8 spatial protocol chip.
//!
//! A program wires the chip's hardware tiles (pins, timers, shifters,
//! counters, logic cells) into a protocol, exports streams the host can use,
//! and carries its own tests:
//!
//! ```
//! let build = spatial::compile(r#"
//!     pin led: out = blink.out
//!     timer blink { every: 1 us, run: 1 }
//! "#)?;
//! assert_eq!(build.pins["led"], 0);
//! assert!(build.image.len() <= 1024);
//! # Ok::<(), spatial::Error>(())
//! ```
//!
//! Everything is checked against what the chip actually has: a program that
//! needs a fifth timer, a ninth pin, or a duration that is not a whole number
//! of clock cycles is rejected with an [`Error`] pointing at the source.

mod byte_pipeline;
mod control;
mod diagnostic;
mod elaborate;
mod hardware;
mod logic;
mod plan;
mod px8;
mod syntax;
mod value;

pub use diagnostic::{Error, Span};
use std::collections::BTreeMap;

/// The spatial RTL revision whose register layout this compiler targets.
pub const HARDWARE_REVISION: &str = "72ffb6458c7de989e2bb48e7d117d7426112a852";

/// A compiled program.
#[derive(Debug)]
pub struct Build {
    /// SRAM image: controller code followed by configuration tables.
    pub image: Vec<u8>,
    /// Physical `uio` pad assigned to each pin.
    pub pins: BTreeMap<String, usize>,
    /// Hardware used, as `(name, used, available)`; `memory` is in bytes.
    pub usage: Vec<(&'static str, usize, usize)>,
    /// Named hardware allocations and the signal-space lines they provide.
    pub resources: Vec<ResourceAssignment>,
    /// Names of the program's tests, in source order.
    pub tests: Vec<String>,
    /// Everything the simulator needs to run the tests, as JSON.
    pub plan: serde_json::Value,
}

/// A source-level resource's physical location in the target tile.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct ResourceAssignment {
    /// Source name, or a generated name for an implicit resource such as an edge cell.
    pub name: String,
    /// Physical resource class, such as `timer` or `logic cell`.
    pub kind: &'static str,
    /// Physical tile index; pin cells use the actual pad number.
    pub index: usize,
    /// First configuration address for this resource, when it has one.
    pub config: Option<u8>,
    /// Named signal-space outputs as `(output name, line number)`.
    pub outputs: Vec<(&'static str, u8)>,
}

/// Parse, check and compile a program.
///
/// # Errors
/// Returns the first problem found, with the source range it concerns.
pub fn compile(source: &str) -> Result<Build, Error> {
    let items = syntax::parse(source)?;
    let design = elaborate::elaborate(&items)?;
    let plan = plan::plan(&design, source)?;
    Ok(Build {
        tests: design
            .tests
            .iter()
            .map(|(name, _, _)| name.text.clone())
            .collect(),
        usage: design.usage,
        resources: design.resources,
        pins: design.pins,
        image: design.image,
        plan,
    })
}
