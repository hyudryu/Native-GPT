//! AgentGPT Desktop host: tracing, sidecar supervisor, embedded axum server,
//! and (unless `--headless`) a Tauri webview pointed at the local server.

use std::time::Duration;

use agentgpt_server::ServerConfig;
use tracing_subscriber::EnvFilter;

const DEFAULT_PORT: u16 = 0; // OS-assigned high port
const DEFAULT_IDLE_TIMEOUT_SECS: u64 = 600;

/// Parsed command-line arguments.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct CliArgs {
    /// Run the server without initializing Tauri or opening a window.
    pub headless: bool,
    /// TCP port; 0 = OS-assigned.
    pub port: u16,
    /// Bind 0.0.0.0 instead of localhost + Tailscale only.
    pub bind_all: bool,
    /// Sidecar idle timeout.
    pub idle_timeout: Duration,
}

/// Outcome of parsing CLI args.
#[derive(Debug, PartialEq, Eq)]
pub enum ParseError {
    /// `--help` was requested.
    Help,
    Error(String),
}

const USAGE: &str = "AgentGPT Desktop host

USAGE:
    agentgpt-host [OPTIONS]

OPTIONS:
    --headless              Run the server without opening a desktop window
    --port <N>              Port to bind (0 = OS-assigned; default 0)
    --bind-all              Bind 0.0.0.0 (warning: reachable beyond the tailnet)
    --idle-timeout-secs <N> Sidecar idle shutdown timeout (default 600)
    -h, --help              Print this help

ENVIRONMENT:
    AGENTGPT_TOKEN          Auth token for non-localhost requests
    AGENTGPT_SIDECAR_CMD    Sidecar spawn command override
    AGENTGPT_REPO_ROOT      Repo root override (for apps/ui/dist + sidecar cwd)
    RUST_LOG                Tracing filter (default: info)";

pub fn parse_args<I>(args: I) -> Result<CliArgs, ParseError>
where
    I: IntoIterator<Item = String>,
{
    let mut parsed = CliArgs {
        headless: false,
        port: DEFAULT_PORT,
        bind_all: false,
        idle_timeout: Duration::from_secs(DEFAULT_IDLE_TIMEOUT_SECS),
    };
    let mut args = args.into_iter();
    while let Some(arg) = args.next() {
        let (flag, inline_value) = match arg.split_once('=') {
            Some((flag, value)) => (flag.to_string(), Some(value.to_string())),
            None => (arg, None),
        };
        let mut value = |name: &str| -> Result<String, ParseError> {
            match inline_value.clone() {
                Some(v) => Ok(v),
                None => args
                    .next()
                    .ok_or_else(|| ParseError::Error(format!("{name} requires a value"))),
            }
        };
        match flag.as_str() {
            "--headless" => parsed.headless = true,
            "--bind-all" => parsed.bind_all = true,
            "--port" => {
                let v = value("--port")?;
                parsed.port = v
                    .parse()
                    .map_err(|_| ParseError::Error(format!("invalid port: {v}")))?;
            }
            "--idle-timeout-secs" => {
                let v = value("--idle-timeout-secs")?;
                let secs: u64 = v
                    .parse()
                    .map_err(|_| ParseError::Error(format!("invalid idle timeout: {v}")))?;
                parsed.idle_timeout = Duration::from_secs(secs);
            }
            "-h" | "--help" => return Err(ParseError::Help),
            other => return Err(ParseError::Error(format!("unknown argument: {other}"))),
        }
    }
    Ok(parsed)
}

fn init_tracing() {
    let filter = EnvFilter::try_from_default_env().unwrap_or_else(|_| EnvFilter::new("info"));
    tracing_subscriber::fmt().with_env_filter(filter).init();
}

/// Entry point called from `main`.
pub fn run() {
    let args = match parse_args(std::env::args().skip(1)) {
        Ok(args) => args,
        Err(ParseError::Help) => {
            println!("{USAGE}");
            return;
        }
        Err(ParseError::Error(msg)) => {
            eprintln!("error: {msg}\n\n{USAGE}");
            std::process::exit(2);
        }
    };
    init_tracing();

    let config = ServerConfig {
        port: args.port,
        bind_all: args.bind_all,
        idle_timeout: args.idle_timeout,
        ..ServerConfig::default()
    };

    let runtime = tokio::runtime::Builder::new_multi_thread()
        .enable_all()
        .build()
        .expect("failed to build tokio runtime");

    if args.headless {
        // IMPORTANT: headless mode never touches Tauri, so it runs on
        // machines without a webview (mobile-only / dev servers).
        tracing::info!("running headless (no desktop window)");
        if let Err(e) = runtime.block_on(agentgpt_server::run(config)) {
            eprintln!("server error: {e:#}");
            std::process::exit(1);
        }
        return;
    }

    let bound = match runtime.block_on(agentgpt_server::bind(config)) {
        Ok(bound) => bound,
        Err(e) => {
            eprintln!("failed to start server: {e:#}");
            std::process::exit(1);
        }
    };
    let port = bound.port;
    runtime.spawn(async move {
        if let Err(e) = bound.wait().await {
            tracing::error!("server error: {e:#}");
        }
    });
    run_desktop(port);
}

fn run_desktop(port: u16) {
    let url = format!("http://127.0.0.1:{port}");
    tauri::Builder::default()
        .setup(move |app| {
            tauri::WebviewWindowBuilder::new(
                app,
                "main",
                tauri::WebviewUrl::External(url.parse().expect("local URL is valid")),
            )
            .title("AgentGPT")
            .decorations(false)
            .inner_size(1280.0, 800.0)
            .build()?;
            Ok(())
        })
        .run(tauri::generate_context!())
        .expect("error while running AgentGPT desktop");
}

#[cfg(test)]
mod tests {
    use super::*;

    fn parse(args: &[&str]) -> Result<CliArgs, ParseError> {
        parse_args(args.iter().map(|s| s.to_string()))
    }

    #[test]
    fn defaults() {
        let args = parse(&[]).unwrap();
        assert!(!args.headless);
        assert_eq!(args.port, 0);
        assert!(!args.bind_all);
        assert_eq!(args.idle_timeout, Duration::from_secs(600));
    }

    #[test]
    fn headless_and_flags() {
        let args = parse(&["--headless", "--port", "8080", "--bind-all"]).unwrap();
        assert!(args.headless);
        assert_eq!(args.port, 8080);
        assert!(args.bind_all);
    }

    #[test]
    fn equals_form_and_idle_timeout() {
        let args = parse(&["--port=9000", "--idle-timeout-secs=30"]).unwrap();
        assert_eq!(args.port, 9000);
        assert_eq!(args.idle_timeout, Duration::from_secs(30));
    }

    #[test]
    fn help_and_unknown() {
        assert_eq!(parse(&["--help"]), Err(ParseError::Help));
        assert!(matches!(parse(&["--nope"]), Err(ParseError::Error(_))));
        assert!(matches!(parse(&["--port"]), Err(ParseError::Error(_))));
        assert!(matches!(
            parse(&["--port", "abc"]),
            Err(ParseError::Error(_))
        ));
    }
}
