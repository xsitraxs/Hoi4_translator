//! The window.
//!
//! There is no GUI toolkit here on purpose: the tool must build on a machine
//! that only has a browser and a Rust toolchain, and a native toolkit would add
//! hundreds of dependencies and a system library per platform. Instead
//! [`serve`] starts a tiny HTTP server bound to this machine, prints one URL
//! with a random token, and serves a single page. The page is the window; the
//! work is done by [`crate::job`], so the console and the window cannot drift.
//!
//! Everything is `std`: the server handles one request per connection, sends
//! `Connection: close`, and never keeps state outside [`Shared`]. The token in
//! the URL is what keeps other pages on the machine from driving the tool —
//! a browser sends a `POST` from any open site, but it cannot read the token.

use std::collections::HashMap;
use std::io::{BufRead, BufReader, Read, Write};
use std::net::{TcpListener, TcpStream};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};
use std::time::Duration;

use anyhow::{Context, Result};
use serde_json::{json, Value};

use crate::config::{self, MAX_CONCURRENCY, VERSION};
use crate::job::{self, JobEvent, JobSettings, Sink};
use crate::localisation::HOI4_TARGET_LANGS;
use crate::pipeline::BROKEN_LINE_MODES;
use crate::providers::ENGINES;

/// The page, served from the binary so the executable can be moved alone.
const INDEX_HTML: &str = include_str!("../assets/gui/index.html");

/// Largest request body accepted; the settings JSON is a few hundred bytes.
const MAX_BODY: usize = 256 * 1024;
/// Events kept for the page; older ones are dropped and the log says so.
const MAX_EVENTS: usize = 20_000;
/// How many events to drop at once when the buffer is full.
const PRUNE: usize = 2_000;

/// A finished response, independent of the socket so it can be tested.
pub struct Response {
    pub status: u16,
    pub content_type: &'static str,
    pub body: String,
}

impl Response {
    fn text(status: u16, body: impl Into<String>) -> Self {
        let mut body = body.into();
        if !body.ends_with('\n') {
            body.push('\n');
        }
        Self {
            status,
            content_type: "text/plain; charset=utf-8",
            body,
        }
    }

    fn html(body: &'static str) -> Self {
        Self {
            status: 200,
            content_type: "text/html; charset=utf-8",
            body: body.to_string(),
        }
    }

    fn json(status: u16, value: &Value) -> Self {
        let body = serde_json::to_string(value)
            .unwrap_or_else(|error| format!("{{\"error\":\"{error}\"}}"));
        Self {
            status,
            content_type: "application/json; charset=utf-8",
            body,
        }
    }

    fn error(status: u16, message: &str) -> Self {
        Self::json(status, &json!({ "error": message }))
    }
}

/// Events of one run, and the fact that a run is going on.
#[derive(Default)]
struct Log {
    events: Vec<JobEvent>,
    /// How many events were dropped from the front; `since` is absolute.
    base: usize,
}

/// What the HTTP side and the worker thread share.
#[derive(Default)]
pub struct Shared {
    log: Mutex<Log>,
    running: AtomicBool,
    stop: AtomicBool,
}

impl Shared {
    fn push(&self, event: JobEvent) {
        let mut log = self.log.lock().unwrap_or_else(|p| p.into_inner());
        log.events.push(event);
        if log.events.len() > MAX_EVENTS {
            let excess = log.events.len() - MAX_EVENTS + PRUNE;
            log.events.drain(0..excess);
            log.base += excess;
        }
    }

    fn snapshot(&self, since: usize) -> Value {
        let log = self.log.lock().unwrap_or_else(|p| p.into_inner());
        let start = since.saturating_sub(log.base).min(log.events.len());
        json!({
            "running": self.running.load(Ordering::SeqCst),
            "base": log.base,
            "next": log.base + log.events.len(),
            "events": &log.events[start..],
        })
    }

    /// Forget the previous run and mark a new one as started.
    fn begin(&self) {
        {
            let mut log = self.log.lock().unwrap_or_else(|p| p.into_inner());
            log.events.clear();
            log.base = 0;
        }
        self.stop.store(false, Ordering::SeqCst);
        self.running.store(true, Ordering::SeqCst);
    }

    fn finish(&self) {
        self.running.store(false, Ordering::SeqCst);
    }
}

/// One window session: the shared state plus the token that unlocks it.
pub struct AppState {
    token: String,
    shared: Arc<Shared>,
    quit: AtomicBool,
}

impl AppState {
    pub fn new(token: String) -> Self {
        Self {
            token,
            shared: Arc::new(Shared::default()),
            quit: AtomicBool::new(false),
        }
    }

    /// Compare the token without leaking its length through timing.
    fn token_matches(&self, candidate: &str) -> bool {
        let expected = self.token.as_bytes();
        let candidate = candidate.as_bytes();
        let mut difference = expected.len() ^ candidate.len();
        for (index, byte) in expected.iter().enumerate() {
            let other = candidate.get(index).copied().unwrap_or(0);
            difference |= usize::from(byte ^ other);
        }
        difference == 0
    }

    fn shared(&self) -> Arc<Shared> {
        Arc::clone(&self.shared)
    }
}

/// Everything a run reports as data, for the page to render.
fn meta() -> Value {
    let targets: Vec<Value> = HOI4_TARGET_LANGS
        .iter()
        .map(|(code, name)| json!({ "code": code, "name": name }))
        .collect();
    let broken_lines: Vec<Value> = BROKEN_LINE_MODES
        .iter()
        .map(|(mode, label)| json!({ "value": job::broken_lines_value(*mode), "label": label }))
        .collect();
    json!({
        "version": VERSION,
        "targets": targets,
        "engines": ENGINES,
        "broken_lines": broken_lines,
        "concurrency": config::DEFAULT_CONCURRENCY,
        "max_concurrency": MAX_CONCURRENCY,
        "cache": config::cache_file().display().to_string(),
    })
}

/// Send a job to a worker thread; the page follows it through `/api/state`.
fn spawn_run(shared: Arc<Shared>, settings: JobSettings) {
    shared.begin();
    let sink: Sink = {
        let shared = Arc::clone(&shared);
        Arc::new(move |event: JobEvent| shared.push(event))
    };
    std::thread::spawn(move || {
        // The event stream already carries the result; this only closes the gate.
        job::run(&settings, &sink, &shared.stop);
        shared.finish();
    });
}

/// Answer one request. Pure: no socket, so the tests can call it directly.
pub fn route(state: &AppState, method: &str, target: &str, body: &str) -> Response {
    let (path, query) = target.split_once('?').unwrap_or((target, ""));
    let query = parse_query(query);
    let token = query.get("token").map(String::as_str).unwrap_or("");
    if !state.token_matches(token) {
        return Response::text(
            401,
            "Нужен ключ доступа: откройте адрес, который напечатала программа.",
        );
    }

    match (method, path) {
        ("GET", "/") | ("GET", "/index.html") => Response::html(INDEX_HTML),
        ("GET", "/api/meta") => Response::json(200, &meta()),
        ("GET", "/api/state") => {
            let since = query
                .get("since")
                .and_then(|value| value.parse::<usize>().ok())
                .unwrap_or(0);
            Response::json(200, &state.shared.snapshot(since))
        }
        ("POST", "/api/run") => {
            if state.shared.running.load(Ordering::SeqCst) {
                Response::error(409, "Перевод уже идёт: дождитесь конца или остановите его")
            } else {
                match serde_json::from_str::<JobSettings>(body) {
                    Ok(settings) => {
                        spawn_run(state.shared(), settings);
                        Response::json(200, &json!({ "ok": true }))
                    }
                    Err(error) => {
                        Response::error(400, &format!("Не удалось разобрать настройки: {error}"))
                    }
                }
            }
        }
        ("POST", "/api/cancel") => {
            state.shared.stop.store(true, Ordering::SeqCst);
            Response::json(200, &json!({ "ok": true }))
        }
        ("POST", "/api/quit") => {
            state.quit.store(true, Ordering::SeqCst);
            Response::json(200, &json!({ "ok": true }))
        }
        _ => Response::error(404, "Неизвестный адрес"),
    }
}

fn parse_query(query: &str) -> HashMap<String, String> {
    let mut parsed = HashMap::new();
    for pair in query.split('&').filter(|pair| !pair.is_empty()) {
        let (key, value) = pair.split_once('=').unwrap_or((pair, ""));
        parsed.insert(unescape(key), unescape(value));
    }
    parsed
}

/// Percent-decoding for query values; enough for paths that contain spaces.
fn unescape(value: &str) -> String {
    let bytes = value.as_bytes();
    let mut decoded = Vec::with_capacity(bytes.len());
    let mut index = 0;
    while index < bytes.len() {
        match bytes[index] {
            b'+' => {
                decoded.push(b' ');
                index += 1;
            }
            b'%' if index + 2 < bytes.len() => {
                let hex = std::str::from_utf8(&bytes[index + 1..index + 3]).unwrap_or("");
                match u8::from_str_radix(hex, 16) {
                    Ok(byte) => {
                        decoded.push(byte);
                        index += 3;
                    }
                    Err(_) => {
                        decoded.push(bytes[index]);
                        index += 1;
                    }
                }
            }
            byte => {
                decoded.push(byte);
                index += 1;
            }
        }
    }
    String::from_utf8_lossy(&decoded).to_string()
}

/// A token nobody else on the machine can guess: time, pid and address hashed.
fn new_token() -> String {
    use sha2::{Digest, Sha256};
    let mut hasher = Sha256::new();
    hasher.update(std::process::id().to_le_bytes());
    if let Ok(now) = std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH) {
        hasher.update(now.as_nanos().to_le_bytes());
    }
    let marker = 0u8;
    hasher.update(format!("{:p}", &marker).as_bytes());
    let digest = hasher.finalize();
    digest
        .iter()
        .take(16)
        .map(|byte| format!("{byte:02x}"))
        .collect()
}

/// Serve the window until the page asks to quit or the process is stopped.
///
/// `host` is `127.0.0.1` unless the user asked otherwise; binding to
/// `0.0.0.0` is possible but the token is the only gate, so it stays a choice.
pub fn serve(host: &str, port: u16, open_browser: bool) -> Result<()> {
    let token = new_token();
    let state = Arc::new(AppState::new(token.clone()));
    let listener = bind_listener(host, port)?;
    let address = listener.local_addr().context("адрес сокета")?;
    let reachable = if address.ip().is_unspecified() {
        "127.0.0.1".to_string()
    } else {
        address.ip().to_string()
    };
    let url = format!("http://{reachable}:{}/?token={}", address.port(), token);
    println!("Окно переводчика: {url}");
    println!("Этот адрес — ключ доступа: не публикуйте его. Ctrl+C закрывает сервер.");
    if open_browser {
        if let Err(error) = open_browser_at(&url) {
            log::warn!("Не удалось открыть браузер: {error}. Откройте адрес вручную.");
        }
    }

    // Non-blocking accept so that `/api/quit` can end the loop: the flag is set
    // by a request thread, and a blocking accept would never look at it again.
    listener
        .set_nonblocking(true)
        .context("не удалось перевести сокет в неблокирующий режим")?;
    loop {
        if state.quit.load(Ordering::SeqCst) {
            break;
        }
        match listener.accept() {
            Ok((stream, _peer)) => {
                // On Unix the accepted socket inherits the listener's mode.
                let _ = stream.set_nonblocking(false);
                let state = Arc::clone(&state);
                std::thread::spawn(move || {
                    if let Err(error) = handle(stream, &state) {
                        log::debug!("Соединение закрыто: {error}");
                    }
                });
            }
            Err(error) if error.kind() == std::io::ErrorKind::WouldBlock => {
                std::thread::sleep(Duration::from_millis(50));
            }
            Err(error) => log::debug!("Соединение не принято: {error}"),
        }
    }
    println!("Сервер остановлен.");
    Ok(())
}

/// Take the requested port, or any free one when it is already in use.
///
/// A fixed port makes the address bookmarkable, so it is the default; a second
/// copy of the program must still start instead of refusing to open a window.
fn bind_listener(host: &str, port: u16) -> Result<TcpListener> {
    match TcpListener::bind((host, port)) {
        Ok(listener) => Ok(listener),
        Err(error) if port != 0 => {
            log::warn!("Порт {port} занят ({error}), беру свободный");
            TcpListener::bind((host, 0)).with_context(|| {
                format!("не удалось занять ни порт {port}, ни свободный на {host}")
            })
        }
        Err(error) => Err(error).with_context(|| format!("не удалось занять порт на {host}")),
    }
}

fn handle(mut stream: TcpStream, state: &AppState) -> Result<()> {
    let _ = stream.set_read_timeout(Some(Duration::from_secs(15)));
    let _ = stream.set_write_timeout(Some(Duration::from_secs(15)));
    let mut reader = BufReader::new(stream.try_clone().context("копия сокета")?);

    let mut request_line = String::new();
    if reader.read_line(&mut request_line)? == 0 {
        return Ok(());
    }
    let mut parts = request_line.split_whitespace();
    let method = parts.next().unwrap_or("").to_string();
    let target = parts.next().unwrap_or("/").to_string();

    let mut content_length = 0usize;
    loop {
        let mut header = String::new();
        if reader.read_line(&mut header)? == 0 {
            break;
        }
        let header = header.trim_end();
        if header.is_empty() {
            break;
        }
        if let Some((name, value)) = header.split_once(':') {
            if name.eq_ignore_ascii_case("content-length") {
                content_length = value.trim().parse().unwrap_or(0);
            }
        }
    }

    let mut body = vec![0u8; content_length.min(MAX_BODY)];
    if !body.is_empty() {
        reader.read_exact(&mut body).context("тело запроса")?;
    }
    let body = String::from_utf8_lossy(&body).to_string();

    let response = route(state, &method, &target, &body);
    write_response(&mut stream, &response)
}

fn write_response(stream: &mut TcpStream, response: &Response) -> Result<()> {
    let reason = match response.status {
        200 => "OK",
        400 => "Bad Request",
        401 => "Unauthorized",
        404 => "Not Found",
        409 => "Conflict",
        _ => "Error",
    };
    let head = format!(
        "HTTP/1.1 {} {reason}\r\nContent-Type: {}\r\nContent-Length: {}\r\n\
         Cache-Control: no-store\r\nX-Content-Type-Options: nosniff\r\n\
         Connection: close\r\n\r\n",
        response.status,
        response.content_type,
        response.body.len()
    );
    stream.write_all(head.as_bytes())?;
    stream.write_all(response.body.as_bytes())?;
    stream.flush()?;
    Ok(())
}

/// Hand the URL to the desktop's default browser, best effort.
fn open_browser_at(url: &str) -> Result<()> {
    let mut command = if cfg!(target_os = "windows") {
        let mut command = std::process::Command::new("cmd");
        command.args(["/C", "start", "", url]);
        command
    } else if cfg!(target_os = "macos") {
        let mut command = std::process::Command::new("open");
        command.arg(url);
        command
    } else {
        let mut command = std::process::Command::new("xdg-open");
        command.arg(url);
        command
    };
    command
        .stdin(std::process::Stdio::null())
        .stdout(std::process::Stdio::null())
        .stderr(std::process::Stdio::null())
        .spawn()
        .context("запуск браузера")?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    fn state() -> AppState {
        AppState::new("secret".to_string())
    }

    #[test]
    fn the_page_needs_the_token() {
        let state = state();
        assert_eq!(route(&state, "GET", "/", "").status, 401);
        assert_eq!(route(&state, "GET", "/?token=wrong", "").status, 401);
        let page = route(&state, "GET", "/?token=secret", "");
        assert_eq!(page.status, 200);
        assert!(page.content_type.starts_with("text/html"));
        assert!(page.body.contains("<html"), "the page itself is served");
    }

    #[test]
    fn the_token_is_compared_in_constant_time_shape() {
        let state = state();
        assert!(state.token_matches("secret"));
        assert!(!state.token_matches("secre"));
        assert!(!state.token_matches("secretsecret"));
        assert!(!state.token_matches(""));
    }

    #[test]
    fn meta_lists_what_the_page_must_offer() {
        let state = state();
        let response = route(&state, "GET", "/api/meta?token=secret", "");
        assert_eq!(response.status, 200);
        let value: Value = serde_json::from_str(&response.body).expect("json");
        let targets = value["targets"].as_array().expect("targets");
        assert!(targets.iter().any(|target| target["code"] == "ru"));
        let engines = value["engines"].as_array().expect("engines");
        assert!(engines.iter().any(|engine| engine == "Google"));
        let modes = value["broken_lines"].as_array().expect("modes");
        assert!(modes.iter().any(|mode| mode["value"] == "reject"));
        assert_eq!(value["version"], json!(VERSION));
    }

    #[test]
    fn the_event_stream_resumes_from_a_position() {
        let state = state();
        let shared = state.shared();
        for index in 0..3 {
            shared.push(JobEvent::Log {
                level: "info".to_string(),
                message: format!("строка {index}"),
            });
        }
        let value: Value =
            serde_json::from_str(&route(&state, "GET", "/api/state?since=1&token=secret", "").body)
                .expect("json");
        assert_eq!(value["next"], json!(3));
        assert_eq!(value["base"], json!(0));
        assert_eq!(value["events"].as_array().expect("events").len(), 2);
        assert_eq!(value["running"], json!(false));
    }

    #[test]
    fn a_second_run_is_refused_while_one_is_going() {
        let state = state();
        state.shared.running.store(true, Ordering::SeqCst);
        let response = route(&state, "POST", "/api/run?token=secret", "{}");
        assert_eq!(response.status, 409);
    }

    #[test]
    fn broken_settings_are_a_bad_request_not_a_crash() {
        let state = state();
        let response = route(&state, "POST", "/api/run?token=secret", "{not json");
        assert_eq!(response.status, 400);
        assert!(response.body.contains("ошибка") || response.body.contains("разобрать"));
    }

    #[test]
    fn an_unknown_address_is_not_found() {
        let state = state();
        assert_eq!(route(&state, "GET", "/nope?token=secret", "").status, 404);
    }

    #[test]
    fn query_values_survive_spaces_and_percent_escapes() {
        let parsed = parse_query("token=ab%20c&path=C%3A%5Cmods&empty=");
        assert_eq!(parsed.get("token").map(String::as_str), Some("ab c"));
        assert_eq!(parsed.get("path").map(String::as_str), Some("C:\\mods"));
        assert_eq!(parsed.get("empty").map(String::as_str), Some(""));
    }

    #[test]
    fn the_http_layer_answers_a_real_request() {
        let state = state();
        let listener = TcpListener::bind(("127.0.0.1", 0)).expect("listen");
        let address = listener.local_addr().expect("address");
        let worker = std::thread::spawn(move || {
            let (stream, _peer) = listener.accept().expect("accept");
            let _ = handle(stream, &state);
        });

        let mut client = TcpStream::connect(address).expect("connect");
        client
            .set_read_timeout(Some(Duration::from_secs(10)))
            .expect("timeout");
        client
            .write_all(b"GET /api/meta?token=secret HTTP/1.1\r\nHost: local\r\n\r\n")
            .expect("write");
        let mut response = String::new();
        client.read_to_string(&mut response).expect("read");

        assert!(response.starts_with("HTTP/1.1 200 OK"), "{response}");
        assert!(
            response.contains("Content-Type: application/json"),
            "{response}"
        );
        assert!(response.contains("\"engines\""), "{response}");
        let _ = worker.join();
    }

    #[test]
    fn a_token_is_long_and_different_every_time() {
        let first = new_token();
        let second = new_token();
        assert_eq!(first.len(), 32);
        assert_ne!(first, second);
    }
}
