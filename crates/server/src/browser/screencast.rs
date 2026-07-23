//! Per-active-tab screencast frame pump (spec §10.1): every CDP frame is
//! acked, viewers share at most one newest frame (bounded broadcast), stale
//! frames are dropped, and the pump only runs while at least one viewer is
//! subscribed.

use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::Arc;

use base64::Engine;
use tokio::sync::broadcast;
use tracing::debug;

use super::cdp::CdpClient;
use super::protocol::{Frame, FrameFormat, ViewportSize};

/// Broadcast capacity: one in-flight + one newest frame per viewer. Lagging
/// viewers get `RecvError::Lagged` and simply pick up the newest frame —
/// frames are never buffered unboundedly (spec §14.3).
const FRAME_CAPACITY: usize = 2;

/// JPEG quality targets from spec §10.1.
pub const MIN_JPEG_QUALITY: u32 = 70;
pub const MAX_JPEG_QUALITY: u32 = 80;

/// Pick a JPEG quality in the 70–80 band: lower when many viewers share the
/// stream (bandwidth), higher for a single local viewer.
pub fn jpeg_quality(viewer_count: usize) -> u32 {
    match viewer_count {
        0 | 1 => MAX_JPEG_QUALITY,
        2 => 75,
        _ => MIN_JPEG_QUALITY,
    }
}

/// Cap frame dimensions to the viewer viewport (already in device pixels).
pub fn max_dimensions(viewport: &ViewportSize) -> (u32, u32) {
    let width = (viewport.width as f64 * viewport.device_scale_factor).round() as u32;
    let height = (viewport.height as f64 * viewport.device_scale_factor).round() as u32;
    (width.max(1), height.max(1))
}

/// Convert one `Page.screencastFrame` CDP event into a [`Frame`], assigning
/// `frame_id` and returning the CDP-level frame session id that must be acked.
pub fn frame_from_event(params: &serde_json::Value, frame_id: u64) -> Option<(Frame, u64)> {
    let data = params.get("data")?.as_str()?;
    let cdp_frame_session = params.get("sessionId")?.as_u64()?;
    let metadata = params.get("metadata")?;
    let width = metadata.get("deviceWidth")?.as_u64()? as u32;
    let height = metadata.get("deviceHeight")?.as_u64()? as u32;
    let bytes = base64::engine::general_purpose::STANDARD
        .decode(data)
        .ok()?;
    let format = if bytes.starts_with(b"RIFF") {
        FrameFormat::Webp
    } else {
        FrameFormat::Jpeg
    };
    Some((
        Frame {
            frame_id,
            width,
            height,
            format,
            data: bytes.into(),
        },
        cdp_frame_session,
    ))
}

/// Running pump for one attached tab session. Drop (or [`Self::stop`]) to
/// halt; the manager stops the pump when the last viewer leaves and restarts
/// it on viewer connect (spec §10.1 pause/resume).
pub struct ScreencastPump {
    frames: broadcast::Sender<Arc<Frame>>,
    task: tokio::task::JoinHandle<()>,
    frame_counter: Arc<AtomicU64>,
}

impl ScreencastPump {
    /// Start the CDP screencast and pump frames into a broadcast channel.
    pub async fn start(
        cdp: Arc<CdpClient>,
        session_id: String,
        viewport: ViewportSize,
        viewer_count: usize,
    ) -> Result<Self, super::cdp::CdpError> {
        let (max_width, max_height) = max_dimensions(&viewport);
        cdp.start_screencast(
            &session_id,
            "jpeg",
            jpeg_quality(viewer_count),
            max_width,
            max_height,
        )
        .await?;

        let (frames, _) = broadcast::channel(FRAME_CAPACITY);
        let frame_counter = Arc::new(AtomicU64::new(1));
        let mut events = cdp.subscribe();
        let pump_frames = frames.clone();
        let pump_counter = Arc::clone(&frame_counter);
        let pump_cdp = Arc::clone(&cdp);
        let pump_session = session_id.clone();
        let task = tokio::spawn(async move {
            loop {
                match events.recv().await {
                    Ok(event) => {
                        if event.method != "Page.screencastFrame"
                            || event.session_id.as_deref() != Some(pump_session.as_str())
                        {
                            continue;
                        }
                        let frame_id = pump_counter.fetch_add(1, Ordering::Relaxed);
                        let Some((frame, cdp_frame_session)) =
                            frame_from_event(&event.params, frame_id)
                        else {
                            continue;
                        };
                        // Ack first so Chromium keeps producing frames even if
                        // no viewer is currently receiving.
                        let ack_cdp = Arc::clone(&pump_cdp);
                        let ack_session = pump_session.clone();
                        tokio::spawn(async move {
                            if let Err(e) = ack_cdp
                                .screencast_frame_ack(&ack_session, cdp_frame_session)
                                .await
                            {
                                debug!(error = %e, "screencast ack failed");
                            }
                        });
                        // Capacity 2: a lagging viewer drops stale frames.
                        let _ = pump_frames.send(Arc::new(frame));
                    }
                    Err(broadcast::error::RecvError::Lagged(skipped)) => {
                        debug!(skipped, "screencast pump lagged cdp events");
                    }
                    Err(broadcast::error::RecvError::Closed) => break,
                }
            }
        });
        Ok(Self {
            frames,
            task,
            frame_counter,
        })
    }

    /// Subscribe to frames. Lagging receivers skip to the newest frame.
    pub fn subscribe(&self) -> broadcast::Receiver<Arc<Frame>> {
        self.frames.subscribe()
    }

    /// Next frame id (for tests and diagnostics).
    pub fn next_frame_id(&self) -> u64 {
        self.frame_counter.load(Ordering::Relaxed)
    }

    pub async fn stop(self) {
        self.task.abort();
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn jpeg_quality_stays_in_spec_band() {
        assert_eq!(jpeg_quality(1), 80);
        assert_eq!(jpeg_quality(2), 75);
        assert_eq!(jpeg_quality(5), 70);
        for n in 0..10 {
            assert!((MIN_JPEG_QUALITY..=MAX_JPEG_QUALITY).contains(&jpeg_quality(n)));
        }
    }

    #[test]
    fn max_dimensions_apply_device_scale_factor() {
        let viewport = ViewportSize {
            width: 640,
            height: 400,
            device_scale_factor: 2.0,
        };
        assert_eq!(max_dimensions(&viewport), (1280, 800));
    }

    #[test]
    fn frame_from_event_parses_cdp_shape() {
        let jpeg = [0xFF, 0xD8, 0xFF, 0xE0, 1, 2, 3];
        let params = json!({
            "data": base64::engine::general_purpose::STANDARD.encode(jpeg),
            "sessionId": 9,
            "metadata": {"deviceWidth": 800, "deviceHeight": 600}
        });
        let (frame, cdp_session) = frame_from_event(&params, 42).expect("frame");
        assert_eq!(frame.frame_id, 42);
        assert_eq!((frame.width, frame.height), (800, 600));
        assert_eq!(frame.format, FrameFormat::Jpeg);
        assert_eq!(cdp_session, 9);
        assert_eq!(frame.data.as_ref(), jpeg);

        assert!(frame_from_event(&json!({"data": "??"}), 1).is_none());
        assert!(frame_from_event(&json!({}), 1).is_none());
    }

    #[test]
    fn broadcast_drops_stale_frames() {
        let (tx, mut rx) = broadcast::channel::<Arc<Frame>>(FRAME_CAPACITY);
        for id in 0..5u64 {
            let frame = Frame {
                frame_id: id,
                width: 1,
                height: 1,
                format: FrameFormat::Jpeg,
                data: axum::body::Bytes::new(),
            };
            tx.send(Arc::new(frame)).unwrap();
        }
        // A slow viewer skips ahead: after draining, it holds the newest
        // frame and the channel never grows unboundedly.
        let mut last = None;
        loop {
            match rx.try_recv() {
                Ok(frame) => last = Some(frame),
                Err(broadcast::error::TryRecvError::Lagged(_)) => continue,
                Err(_) => break,
            }
        }
        assert_eq!(last.map(|f| f.frame_id), Some(4));
    }
}
