//! Fixture crate for invariant-receipt (spec §9).
//!
//! Every test here exists to produce one verdict when a named mutation is
//! applied. Nothing in this file is product code.

use std::fs;
use std::path::PathBuf;

/// Holds registered IDs. The invariant: registering the same ID twice is
/// rejected, and a rejected registration stores nothing.
#[derive(Debug, Default)]
pub struct Registry {
    seen: Vec<u32>,
}

impl Registry {
    pub fn new() -> Self {
        Self::default()
    }

    pub fn register(&mut self, id: u32) -> Result<(), String> {
        if self.seen.contains(&id) {
            return Err(format!("duplicate id {id}"));
        }
        self.seen.push(id);
        Ok(())
    }

    pub fn len(&self) -> usize {
        self.seen.len()
    }

    pub fn is_empty(&self) -> bool {
        self.seen.is_empty()
    }
}

/// Clamp a percentage into 0..=100.
pub fn clamp_percent(v: i32) -> i32 {
    if v < 0 {
        0
    } else if v > 100 {
        100
    } else {
        v
    }
}

/// Decide whether this invocation of the flaky test should fail.
///
/// With `IR_FIXTURE_FLAKY_DIR` set, the test keeps a counter file there and
/// fails on every second invocation — genuinely flaky across runs, but
/// deterministic, so invariant-receipt's own tests can rely on it. Without
/// the variable it flips on the clock, which is what real flakiness looks like.
pub fn flaky_should_fail() -> bool {
    match std::env::var_os("IR_FIXTURE_FLAKY_DIR") {
        Some(dir) => {
            let path = PathBuf::from(dir).join("rust-flaky-counter");
            let n: u64 = fs::read_to_string(&path)
                .ok()
                .and_then(|s| s.trim().parse().ok())
                .unwrap_or(0)
                + 1;
            fs::write(&path, n.to_string()).expect("write flaky counter");
            n % 2 == 0
        }
        None => {
            let nanos = std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .map(|d| d.subsec_nanos())
                .unwrap_or(0);
            nanos % 2 == 0
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    /// The good test: checks both halves of the invariant.
    #[test]
    fn rejects_duplicate_id() {
        let mut r = Registry::new();
        assert_eq!(r.register(7), Ok(()), "first registration must succeed");
        let err = r
            .register(7)
            .expect_err("second registration must be rejected");
        assert!(err.contains("duplicate"), "wrong error: {err}");
        assert_eq!(r.len(), 1, "registry must hold one entry");
    }

    /// The weak test: never registers twice, so it cannot see the rule break.
    #[test]
    fn register_accepts_new_id() {
        let mut r = Registry::new();
        assert!(r.register(7).is_ok());
    }

    #[test]
    fn clamps_high_values() {
        assert_eq!(clamp_percent(250), 100);
    }

    #[test]
    #[should_panic(expected = "must be rejected")]
    fn duplicate_panics_when_unwrapped() {
        let mut r = Registry::new();
        r.register(3).unwrap();
        r.register(3).expect_err("must be rejected");
        panic!("must be rejected");
    }

    #[test]
    fn flaky_on_alternate_runs() {
        assert!(!flaky_should_fail(), "flaky test failed this time");
    }

    /// Selected together with `register_accepts_new_id` by that name's
    /// substring filter. When the rule breaks THIS test fails while the weak
    /// one passes: the run exits non-zero, and the verdict must still be
    /// SURVIVED, because only the named test's own result counts.
    /// Passes. Its name also selects `sibling_green_neighbour_is_red`, which
    /// always fails: the baseline run is NOT fully green, so no mutation of an
    /// invariant naming this test may be credited (Codex review, finding 2).
    #[test]
    fn sibling_green() {
        assert_eq!(clamp_percent(-3), 0);
    }

    #[test]
    fn sibling_green_neighbour_is_red() {
        panic!("this sibling is red on the unmodified code");
    }

    #[test]
    fn register_accepts_new_id_then_rejects_it_again() {
        let mut r = Registry::new();
        assert!(r.register(9).is_ok());
        assert!(r.register(9).is_err(), "a repeat must be rejected");
    }
}
