//! Telemetry for the host and sidecar processes (ADR-0004 watchdog data).
//!
//! Backed by `sysinfo`. NVIDIA GPU sampling via NVML is planned behind the
//! `nvml` cargo feature; it currently returns [`GpuSample::Unavailable`].

use std::sync::{Mutex, MutexGuard};

use sysinfo::{Pid, ProcessesToUpdate, System};

/// System-wide memory snapshot (bytes).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct SystemMemory {
    pub total_bytes: u64,
    pub used_bytes: u64,
}

/// GPU telemetry sample. Always `Unavailable` until NVML support lands.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum GpuSample {
    Unavailable,
}

/// Process/system telemetry sampler. Interior-mutable; cheap to share.
pub struct Telemetry {
    system: Mutex<System>,
}

impl Default for Telemetry {
    fn default() -> Self {
        Self::new()
    }
}

fn lock<T>(mutex: &Mutex<T>) -> MutexGuard<'_, T> {
    mutex.lock().unwrap_or_else(|e| e.into_inner())
}

impl Telemetry {
    pub fn new() -> Self {
        Self {
            system: Mutex::new(System::new()),
        }
    }

    /// RSS of the current (host) process in bytes.
    pub fn host_rss_bytes(&self) -> u64 {
        self.process_rss_bytes(std::process::id()).unwrap_or(0)
    }

    /// RSS of `pid` in bytes, or `None` if the process does not exist.
    pub fn process_rss_bytes(&self, pid: u32) -> Option<u64> {
        let pid = Pid::from_u32(pid);
        let mut system = lock(&self.system);
        system.refresh_processes(ProcessesToUpdate::Some(&[pid]), true);
        system.process(pid).map(|p| p.memory())
    }

    /// System-wide memory snapshot.
    pub fn system_memory(&self) -> SystemMemory {
        let mut system = lock(&self.system);
        system.refresh_memory();
        SystemMemory {
            total_bytes: system.total_memory(),
            used_bytes: system.used_memory(),
        }
    }

    /// NVIDIA GPU sample via NVML (feature `nvml`). Stub: unavailable.
    pub fn nvidia_gpu_sample(&self) -> GpuSample {
        nvml::sample()
    }
}

#[cfg(feature = "nvml")]
mod nvml {
    use super::GpuSample;

    pub fn sample() -> GpuSample {
        // NVML wiring lands with the GPU watchdog phase.
        GpuSample::Unavailable
    }
}

#[cfg(not(feature = "nvml"))]
mod nvml {
    use super::GpuSample;

    pub fn sample() -> GpuSample {
        GpuSample::Unavailable
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn host_rss_is_positive() {
        let telemetry = Telemetry::new();
        assert!(telemetry.host_rss_bytes() > 0);
    }

    #[test]
    fn unknown_pid_returns_none() {
        let telemetry = Telemetry::new();
        // PIDs near u32::MAX do not exist on any supported platform.
        assert_eq!(telemetry.process_rss_bytes(u32::MAX - 1), None);
    }

    #[test]
    fn gpu_stub_is_unavailable() {
        assert_eq!(Telemetry::new().nvidia_gpu_sample(), GpuSample::Unavailable);
    }
}
