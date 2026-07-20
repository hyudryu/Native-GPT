//! Network interface discovery for Tailscale-only binding (ADR-0003).

use std::net::Ipv4Addr;

/// Tailscale CGNAT range: 100.64.0.0/10.
pub fn is_tailscale_cgnat(ip: &Ipv4Addr) -> bool {
    let octets = ip.octets();
    octets[0] == 100 && (64..=127).contains(&octets[1])
}

/// IPv4 addresses of interfaces inside the Tailscale CGNAT range.
/// Enumeration failure (or no Tailscale interface) yields an empty list —
/// callers fall back to localhost-only binding.
pub fn tailscale_ipv4_addrs() -> Vec<Ipv4Addr> {
    let ifaces = match if_addrs::get_if_addrs() {
        Ok(ifaces) => ifaces,
        Err(e) => {
            tracing::warn!("interface enumeration failed ({e}); binding localhost only");
            return Vec::new();
        }
    };
    let mut addrs: Vec<Ipv4Addr> = ifaces
        .into_iter()
        .filter(|i| !i.is_loopback())
        .filter_map(|i| match i.addr {
            if_addrs::IfAddr::V4(v4) if is_tailscale_cgnat(&v4.ip) => Some(v4.ip),
            _ => None,
        })
        .collect();
    addrs.sort_unstable();
    addrs.dedup();
    addrs
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn cgnat_range_boundaries() {
        // 100.64.0.0/10 = 100.64.0.0 ..= 100.127.255.255
        assert!(is_tailscale_cgnat(&Ipv4Addr::new(100, 64, 0, 0)));
        assert!(is_tailscale_cgnat(&Ipv4Addr::new(100, 100, 1, 2)));
        assert!(is_tailscale_cgnat(&Ipv4Addr::new(100, 127, 255, 255)));
        assert!(!is_tailscale_cgnat(&Ipv4Addr::new(100, 63, 255, 255)));
        assert!(!is_tailscale_cgnat(&Ipv4Addr::new(100, 128, 0, 0)));
        assert!(!is_tailscale_cgnat(&Ipv4Addr::new(192, 168, 1, 1)));
        assert!(!is_tailscale_cgnat(&Ipv4Addr::new(10, 0, 0, 1)));
        assert!(!is_tailscale_cgnat(&Ipv4Addr::LOCALHOST));
    }

    #[test]
    fn enumeration_never_panics() {
        // May or may not find a Tailscale interface; must not fail either way.
        let _ = tailscale_ipv4_addrs();
    }
}
