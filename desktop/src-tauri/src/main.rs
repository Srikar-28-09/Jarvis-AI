#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use base64::{engine::general_purpose::STANDARD, Engine};
use futures_util::StreamExt;
use reqwest::{cookie::Jar, multipart, Client, Method, Url};
use serde::Serialize;
use serde_json::{json, Value};
use std::{
    fs::{create_dir_all, OpenOptions},
    io::Write,
    process::{Child, Command, Output, Stdio},
    thread,
    sync::{Arc, Mutex},
    time::{Duration, Instant, SystemTime, UNIX_EPOCH},
};
use tauri::{AppHandle, Emitter, Manager, State, WebviewWindow};
#[cfg(target_os = "linux")]
use tauri::webview::{PermissionKind, PermissionResponse};

struct ApiState {
    client: Mutex<Client>,
    base: Mutex<String>,
    shortcut_diagnostic: Mutex<String>,
    active_shortcut: Mutex<String>,
    owns_backend_service: Mutex<bool>,
    owns_bridge_service: Mutex<bool>,
    process_started_at: Instant,
}

#[tauri::command]
fn desktop_rendered(state: State<'_, ApiState>) {
    let elapsed = state.process_started_at.elapsed().as_secs_f64() * 1000.0;
    write_voice_log("activation", &format!("cold_start_to_frontend_ms={elapsed:.1}"));
}

#[tauri::command]
fn record_activation_metric(name: String, elapsed_ms: f64) -> Result<(), String> {
    const METRICS: &[&str] = &[
        "frontend_first_frame",
        "initial_input_focus",
        "warm_activation_to_focus",
        "backend_service_start_request",
        "backend_ready_after_activation",
        "backend_startup_timeout_after",
        "host_bridge_service_status",
    ];
    if !METRICS.contains(&name.as_str()) || !elapsed_ms.is_finite() || !(0.0..=600_000.0).contains(&elapsed_ms) {
        return Err("Invalid activation metric".into());
    }
    write_voice_log("activation", &format!("{name}_ms={elapsed_ms:.1}"));
    Ok(())
}

#[derive(Serialize)]
struct TtsAudio { bytes: Vec<u8>, mime_type: String }

fn model_stream_failure(frame: &str) -> Option<String> {
    let event_failed = frame
        .lines()
        .any(|line| line.trim_end_matches('\r') == "event: error");
    let payload = frame
        .lines()
        .find_map(|line| line.strip_prefix("data: "))
        .and_then(|data| serde_json::from_str::<Value>(data).ok());
    let payload_failed = payload
        .as_ref()
        .and_then(|value| value.get("type"))
        .and_then(Value::as_str)
        .is_some_and(|kind| matches!(kind, "error" | "stream_error"));
    if !event_failed && !payload_failed {
        return None;
    }

    let status = payload
        .as_ref()
        .and_then(|value| value.get("status"))
        .and_then(Value::as_u64);
    let error = payload
        .as_ref()
        .and_then(|value| value.get("error"))
        .and_then(Value::as_str)
        .unwrap_or("");
    let endpoint = error
        .strip_prefix("Cannot reach ")
        .or_else(|| error.strip_prefix("Upstream "))
        .and_then(|value| value.split_whitespace().next())
        .and_then(|value| Url::parse(value).ok())
        .filter(|url| {
            matches!(url.scheme(), "http" | "https")
                && url.host_str().is_some()
                && url.username().is_empty()
                && url.password().is_none()
        })
        .map(|url| {
            let host = url.host_str().unwrap_or_default();
            match url.port() {
                Some(port) => format!("{}://{host}:{port}", url.scheme()),
                None => format!("{}://{host}", url.scheme()),
            }
        });

    Some(match (status, endpoint) {
        (Some(503), Some(endpoint)) => {
            format!("Model endpoint unavailable at {endpoint}. Check that the configured model service is running.")
        }
        (Some(503), None) => {
            "The configured model endpoint is unavailable (HTTP 503). Check its service status.".into()
        }
        (Some(504), _) => {
            "The configured model endpoint timed out (HTTP 504). Check its service and retry.".into()
        }
        (Some(status), _) => format!("The model request failed (HTTP {status})."),
        (None, _) => "The model request failed while streaming.".into(),
    })
}

fn audio_mime(bytes: &[u8]) -> Option<&'static str> {
    if bytes.len() >= 12 && &bytes[..4] == b"RIFF" && &bytes[8..12] == b"WAVE" {
        Some("audio/wav")
    } else if bytes.starts_with(b"ID3") || (bytes.len() >= 2 && bytes[0] == 0xff && (bytes[1] & 0xe0) == 0xe0) {
        Some("audio/mpeg")
    } else {
        None
    }
}

fn write_voice_log(category: &str, message: &str) {
    if !matches!(category, "webview" | "audio" | "tts" | "activation") { return; }
    let safe_message: String = message.chars()
        .map(|ch| if ch.is_control() { ' ' } else { ch })
        .take(600)
        .collect();
    let timestamp = SystemTime::now().duration_since(UNIX_EPOCH).unwrap_or_default().as_millis();
    let line = format!("{timestamp} [{category}] {safe_message}\n");
    #[cfg(target_os = "linux")]
    {
        let state_home = std::env::var_os("XDG_STATE_HOME")
            .map(std::path::PathBuf::from)
            .or_else(|| std::env::var_os("HOME").map(|home| std::path::PathBuf::from(home).join(".local/state")));
        if let Some(path) = state_home.map(|dir| dir.join("odysseus-voice").join("desktop.log")) {
            if let Some(parent) = path.parent() {
                if create_dir_all(parent).is_ok() {
                    if std::fs::metadata(&path).is_ok_and(|metadata| metadata.len() >= 1_048_576) {
                        let rotated = path.with_extension("log.1");
                        let _ = std::fs::remove_file(&rotated);
                        let _ = std::fs::rename(&path, rotated);
                    }
                    if let Ok(mut file) = OpenOptions::new().create(true).append(true).open(path) {
                        let _ = file.write_all(line.as_bytes());
                        return;
                    }
                }
            }
        }
    }
    eprint!("{line}");
}

#[tauri::command]
fn voice_diagnostic(category: String, message: String) {
    write_voice_log(&category, &message);
}

fn clean_base(value: &str) -> Result<String, String> {
    let url = Url::parse(value.trim()).map_err(|_| "Enter a valid backend URL".to_string())?;
    if !matches!(url.scheme(), "http" | "https") || url.host_str().is_none() {
        return Err("Backend URL must use HTTP or HTTPS".into());
    }
    Ok(value.trim().trim_end_matches('/').to_string())
}

fn build_client() -> Result<Client, String> {
    let jar = Arc::new(Jar::default());
    Client::builder().cookie_provider(jar).timeout(Duration::from_secs(90))
        .build().map_err(|e| format!("Could not initialize secure session client: {e}"))
}

#[cfg(target_os = "linux")]
fn is_trusted_microphone_origin(url: &Url) -> bool {
    match (url.scheme(), url.host_str(), url.port()) {
        ("tauri", Some("localhost"), None) => true,
        ("http", Some("localhost"), Some(1420)) => true,
        _ => false,
    }
}

#[cfg(target_os = "linux")]
fn microphone_permission_response(kind: PermissionKind, trusted_ui: bool) -> PermissionResponse {
    if trusted_ui && kind == PermissionKind::Microphone {
        PermissionResponse::Allow
    } else {
        PermissionResponse::Deny
    }
}

async fn request(state: &ApiState, method: Method, path: &str) -> Result<reqwest::RequestBuilder, String> {
    let client = state.client.lock().map_err(|_| "Session client unavailable".to_string())?.clone();
    let base = state.base.lock().map_err(|_| "Backend URL unavailable".to_string())?.clone();
    Ok(client.request(method, format!("{base}{path}")))
}

async fn checked(response: reqwest::Response) -> Result<reqwest::Response, String> {
    if response.status().is_success() { return Ok(response); }
    let status = response.status();
    let body = response.text().await.unwrap_or_default();
    let detail = serde_json::from_str::<Value>(&body).ok()
        .and_then(|v| v.get("detail").cloned().or_else(|| v.get("error").cloned()))
        .map(|v| if let Some(s) = v.as_str() { s.to_string() } else { v.to_string() })
        .unwrap_or_else(|| "Request failed".into());
    Err(if status.as_u16() == 401 { "Sign in again to continue".into() } else { format!("{detail} ({status})") })
}

#[derive(Serialize)]
struct Status { healthy: bool, authenticated: bool, username: Option<String> }

#[tauri::command]
async fn backend_status(state: State<'_, ApiState>, backend: String) -> Result<Status, String> {
    let base = clean_base(&backend)?;
    *state.base.lock().map_err(|_| "Backend URL unavailable".to_string())? = base;
    let health = request(&state, Method::GET, "/api/health").await?
        .timeout(Duration::from_secs(3))
        .send().await.map_err(|_| "Odysseus backend is unavailable. Check its address and try again.".to_string())?;
    checked(health).await?;
    let auth = request(&state, Method::GET, "/api/auth/status").await?
        .timeout(Duration::from_secs(2))
        .send().await.map_err(|_| "Could not read backend authentication status".to_string())?;
    let value: Value = checked(auth).await?.json().await.map_err(|_| "Could not read authentication status".to_string())?;
    let authenticated = value.get("authenticated").and_then(Value::as_bool).unwrap_or(false);
    if authenticated {
        let readiness = request(&state, Method::GET, "/api/ready").await?
            .timeout(Duration::from_secs(3))
            .send().await.map_err(|_| "Could not check backend readiness".to_string())?;
        checked(readiness).await?;
    }
    Ok(Status {
        healthy: true,
        authenticated,
        username: value.get("username").and_then(Value::as_str).map(str::to_string),
    })
}

#[tauri::command]
async fn login(state: State<'_, ApiState>, backend: String, username: String, password: String, totp_code: Option<String>) -> Result<Value, String> {
    *state.base.lock().map_err(|_| "Backend URL unavailable".to_string())? = clean_base(&backend)?;
    let response = request(&state, Method::POST, "/api/auth/login").await?.json(&json!({"username":username,"password":password,"totp_code":totp_code,"remember":false})).send().await.map_err(|_| "Could not connect to the backend".to_string())?;
    let value: Value = checked(response).await?.json().await.map_err(|_| "Could not read sign-in response".to_string())?;
    Ok(value)
}

#[tauri::command]
async fn get_default_chat_model(state: State<'_, ApiState>) -> Result<Value, String> {
    let response = request(&state, Method::GET, "/api/models/default-chat").await?
        .timeout(Duration::from_secs(5))
        .send().await.map_err(|_| "Could not load the configured default chat model".to_string())?;
    let value: Value = checked(response).await?
        .json().await.map_err(|_| "Could not read the configured default chat model".to_string())?;
    configured_chat_model(&value)
}

fn configured_chat_model(value: &Value) -> Result<Value, String> {
    let endpoint_id = value.get("endpoint_id").and_then(Value::as_str).unwrap_or("");
    if endpoint_id.trim().is_empty() {
        return Err("Configure an enabled default chat model in Odysseus settings before starting a conversation.".into());
    }
    Ok(json!({
        "endpoint_id": endpoint_id,
        "model": value.get("model").and_then(Value::as_str).unwrap_or(""),
    }))
}

#[tauri::command]
async fn stt_status(state: State<'_, ApiState>) -> Result<Value, String> {
    let response = request(&state, Method::GET, "/api/stt/stats").await?
        .timeout(Duration::from_secs(3))
        .send().await.map_err(|_| "Could not check speech-to-text readiness".to_string())?;
    let value: Value = checked(response).await?
        .json().await.map_err(|_| "Could not read speech-to-text status".to_string())?;
    Ok(json!({
        "available": value.get("available").and_then(Value::as_bool).unwrap_or(false),
        "enabled": value.get("enabled").and_then(Value::as_bool).unwrap_or(false),
        "provider": value.get("provider").and_then(Value::as_str).unwrap_or("unknown"),
        "reason": value.get("reason").and_then(Value::as_str).unwrap_or("Speech-to-text is not ready."),
    }))
}

#[tauri::command]
async fn create_voice_session(state: State<'_, ApiState>, endpoint_id: String, model: String) -> Result<String, String> {
    let form = multipart::Form::new().text("name", "Odysseus Voice").text("endpoint_id", endpoint_id).text("model", model);
    let response = request(&state, Method::POST, "/api/session").await?.multipart(form).send().await.map_err(|_| "Could not create a conversation".to_string())?;
    let value: Value = checked(response).await?.json().await.map_err(|_| "Could not read conversation response".to_string())?;
    value.get("id").and_then(Value::as_str).map(str::to_string).ok_or_else(|| "Backend did not return a conversation ID".into())
}

#[tauri::command]
async fn transcribe(state: State<'_, ApiState>, audio_base64: String, mime_type: String, request_id: String) -> Result<String, String> {
    let bytes = STANDARD.decode(audio_base64).map_err(|_| "Could not read recorded audio".to_string())?;
    let mime = mime_type.parse::<mime::Mime>().unwrap_or_else(|_| "audio/wav".parse().expect("valid audio MIME"));
    let file_name = match (mime.type_().as_str(), mime.subtype().as_str()) {
        ("audio", "wav" | "x-wav") => "utterance.wav",
        ("audio", "webm") => "utterance.webm",
        _ => return Err("Unsupported recorded audio format".into()),
    };
    let part = multipart::Part::bytes(bytes).file_name(file_name).mime_str(mime.as_ref()).map_err(|e| e.to_string())?;
    let form = multipart::Form::new().part("file", part);
    let response = request(&state, Method::POST, "/api/stt/transcribe").await?.header("X-Odysseus-Request-Id", request_id).multipart(form).send().await.map_err(|_| "Transcription service is unavailable".to_string())?;
    let value: Value = checked(response).await?.json().await.map_err(|_| "Could not read transcription".to_string())?;
    Ok(value.get("text").and_then(Value::as_str).unwrap_or("").trim().to_string())
}

#[tauri::command]
async fn chat(state: State<'_, ApiState>, app: tauri::AppHandle, session_id: String, message: String, request_id: String) -> Result<String, String> {
    let form = multipart::Form::new().text("session", session_id).text("message", message).text("mode", "jarvis");
    let response = request(&state, Method::POST, "/api/chat_stream").await?.header("X-Odysseus-Request-Id", &request_id).multipart(form).send().await.map_err(|_| "Could not reach the chat service".to_string())?;
    let response = checked(response).await?;
    let run_id = response.headers().get("X-Odysseus-Run-Id").and_then(|v| v.to_str().ok()).unwrap_or("").to_string();
    let _ = app.emit("voice-chat-event", json!({"request_id":request_id,"type":"started","run_id":run_id}));
    let mut stream = response.bytes_stream();
    let mut pending = Vec::<u8>::new();
    let mut answer = String::new();
    while let Some(chunk) = stream.next().await {
        let bytes = chunk.map_err(|_| "Chat stream was interrupted".to_string())?;
        pending.extend_from_slice(&bytes);
        while let Some(end) = pending.windows(2).position(|window| window == b"\n\n") {
            let frame = String::from_utf8_lossy(&pending.drain(..end + 2).collect::<Vec<_>>()).to_string();
            if let Some(message) = model_stream_failure(&frame) {
                let _ = app.emit("voice-chat-event", json!({"request_id":request_id,"type":"error","message":message}));
                return Err(message);
            }
            for line in frame.lines().filter(|line| line.starts_with("data: ")) {
                let data = &line[6..];
                if data == "[DONE]" { continue; }
                let Ok(event) = serde_json::from_str::<Value>(data) else { continue };
                if event.get("thinking").and_then(Value::as_bool).unwrap_or(false) { continue; }
                if let Some(delta) = event.get("delta").and_then(Value::as_str) {
                    answer.push_str(delta);
                    let _ = app.emit("voice-chat-event", json!({"request_id":request_id,"type":"delta","text":delta}));
                }
                let kind = event.get("type").and_then(Value::as_str).unwrap_or("");
                if kind == "model_info" && answer.is_empty() { let _ = app.emit("voice-chat-event", json!({"request_id":request_id,"type":"model","text":"Connected to Odysseus"})); }
            }
        }
    }
    let _ = app.emit("voice-chat-event", json!({"request_id":request_id,"type":"complete"}));
    Ok(answer)
}

#[tauri::command]
async fn cancel_chat(state: State<'_, ApiState>, session_id: String, run_id: Option<String>) -> Result<(), String> {
    let builder = request(&state, Method::POST, &format!("/api/chat/stop/{session_id}")).await?;
    let builder = if let Some(id) = run_id.filter(|id| !id.is_empty()) { builder.header("X-Odysseus-Run-Id", id) } else { builder };
    let response = builder.send().await.map_err(|_| "Could not contact the cancellation endpoint".to_string())?;
    checked(response).await?;
    Ok(())
}

#[tauri::command]
async fn synthesize(state: State<'_, ApiState>, text: String, request_id: String) -> Result<TtsAudio, String> {
    let response = request(&state, Method::POST, "/api/tts/synthesize").await?
        .header("X-Odysseus-Request-Id", request_id)
        .json(&json!({"text":text,"format":"audio"}))
        .send().await.map_err(|_| {
            write_voice_log("tts", "synthesis request failed before an HTTP response");
            "Jarvis voice service is unavailable".to_string()
        })?;
    let response = checked(response).await.map_err(|error| {
        write_voice_log("tts", "synthesis endpoint returned an unsuccessful response");
        error
    })?;
    let bytes = response.bytes().await.map_err(|_| {
        write_voice_log("tts", "could not receive synthesized audio bytes");
        "Could not receive Jarvis audio".to_string()
    })?.to_vec();
    let Some(mime_type) = audio_mime(&bytes) else {
        write_voice_log("tts", &format!("synthesis returned invalid or unsupported audio ({} bytes)", bytes.len()));
        return Err("Jarvis voice service returned an unsupported or invalid audio format".into());
    };
    write_voice_log("tts", &format!("audio received: {} bytes, {mime_type}", bytes.len()));
    Ok(TtsAudio { bytes, mime_type: mime_type.into() })
}

#[tauri::command]
async fn tts_status(state: State<'_, ApiState>) -> Result<Value, String> {
    let response = request(&state, Method::GET, "/api/tts/stats").await?.send().await.map_err(|_| "Could not read Jarvis voice settings".to_string())?;
    checked(response).await?.json().await.map_err(|_| "Could not read Jarvis voice settings".to_string())
}

#[tauri::command]
async fn hide_overlay(window: WebviewWindow) -> Result<(), String> { window.hide().map_err(|e| e.to_string()) }

#[tauri::command]
fn get_shortcut_diagnostic(state: State<'_, ApiState>) -> String {
    state.shortcut_diagnostic.lock().map(|message| message.clone()).unwrap_or_default()
}

fn activate_overlay(app: &AppHandle) {
    let started = Instant::now();
    if let Some(window) = app.get_webview_window("main") {
        if window.is_minimized().unwrap_or(false) {
            let _ = window.unminimize();
        }
        let _ = window.show();
        let _ = window.set_focus();
        let _ = app.emit("ui-activation", ());
        write_voice_log(
            "activation",
            &format!("native_reveal_and_focus_ms={:.1}", started.elapsed().as_secs_f64() * 1000.0),
        );
    }
}

fn toggle_overlay(app: &AppHandle) {
    if let Some(window) = app.get_webview_window("main") {
        if window.is_visible().unwrap_or(false) && !window.is_minimized().unwrap_or(false) {
            let _ = window.hide();
        } else {
            activate_overlay(app);
        }
    }
}

#[cfg(not(target_os = "linux"))]
fn register_activation_shortcut(app: &AppHandle, shortcut: &str) -> Result<(), String> {
    use tauri_plugin_global_shortcut::{GlobalShortcutExt, ShortcutState};
    let handle = app.clone();
    app.global_shortcut().on_shortcut(shortcut, move |_app, _shortcut, event| {
        if event.state == ShortcutState::Pressed { activate_overlay(&handle); }
    }).map_err(|error| error.to_string())
}

#[tauri::command]
fn get_activation_shortcut(state: State<'_, ApiState>) -> String {
    state.active_shortcut.lock().map(|shortcut| shortcut.clone()).unwrap_or_default()
}

#[cfg(target_os = "linux")]
fn update_gnome_shortcut(shortcut: &str) -> Result<(), String> {
    let home = std::env::var_os("HOME").ok_or("Home directory is unavailable")?;
    let installed_helper = std::path::PathBuf::from(home)
        .join(".local/libexec/odysseus-ubuntu-shortcut.py");
    let helper = if installed_helper.is_file() {
        installed_helper
    } else {
        std::path::PathBuf::from(env!("CARGO_MANIFEST_DIR"))
            .join("../../scripts/ubuntu-shortcut.py")
    };
    if !helper.is_file() {
        return Err("GNOME shortcut helper is missing. Run install-ubuntu-desktop.sh.".into());
    }
    let result = Command::new("python3")
        .arg(helper)
        .args(["set", "--shortcut", shortcut])
        .output()
        .map_err(|_| "Could not run the GNOME shortcut helper. Install Python 3.".to_string())?;
    if result.status.success() {
        Ok(())
    } else {
        Err(String::from_utf8_lossy(&result.stderr).trim().to_string())
    }
}

#[tauri::command]
fn set_activation_shortcut(_app: AppHandle, state: State<'_, ApiState>, shortcut: String) -> Result<(), String> {
    let shortcut = shortcut.trim();
    if shortcut.is_empty() || shortcut.len() > 64 {
        return Err("Enter a non-empty shortcut of at most 64 characters".into());
    }

    let mut active = state.active_shortcut.lock().map_err(|_| "Shortcut state is unavailable")?;
    #[cfg(target_os = "linux")]
    {
        update_gnome_shortcut(shortcut)?;
        *active = shortcut.to_owned();
        return Ok(());
    }
    #[cfg(not(target_os = "linux"))]
    {
        if shortcut == active.as_str() {
            return Ok(());
        }
        use tauri_plugin_global_shortcut::GlobalShortcutExt;
    register_activation_shortcut(&_app, shortcut)?;
    if !active.is_empty() {
        if let Err(error) = _app.global_shortcut().unregister(active.as_str()) {
            let _ = _app.global_shortcut().unregister(shortcut);
            return Err(error.to_string());
        }
    }
    *active = shortcut.to_owned();
    if let Ok(mut diagnostic) = state.shortcut_diagnostic.lock() {
        diagnostic.clear();
    }
    Ok(())
    }
}

fn run_bounded(args: &[&str], timeout: Duration) -> Result<Output, String> {
    let mut child: Child = Command::new(args[0])
        .args(&args[1..])
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .map_err(|_| "Required system service command is unavailable".to_string())?;
    let deadline = Instant::now() + timeout;
    loop {
        match child.try_wait() {
            Ok(Some(_)) => return child.wait_with_output().map_err(|_| "Could not read service status".into()),
            Ok(None) if Instant::now() < deadline => thread::sleep(Duration::from_millis(25)),
            Ok(None) => {
                let _ = child.kill();
                let _ = child.wait();
                return Err("System service command timed out".into());
            }
            Err(_) => {
                let _ = child.kill();
                let _ = child.wait();
                return Err("Could not inspect system service command".into());
            }
        }
    }
}

#[cfg(target_os = "linux")]
fn unit_active(unit: &str) -> bool {
    run_bounded(&["systemctl", "--user", "is-active", "--quiet", unit], Duration::from_secs(2))
        .is_ok_and(|output| output.status.success())
}

#[cfg(target_os = "linux")]
fn unit_failed(unit: &str) -> bool {
    run_bounded(&["systemctl", "--user", "is-failed", "--quiet", unit], Duration::from_secs(2))
        .is_ok_and(|output| output.status.success())
}

#[cfg(target_os = "linux")]
fn start_user_unit(unit: &str) -> Result<(), String> {
    let output = run_bounded(
        &["systemctl", "--user", "start", "--no-block", unit],
        Duration::from_secs(3),
    )?;
    if output.status.success() {
        Ok(())
    } else {
        let detail = String::from_utf8_lossy(&output.stderr).trim().to_string();
        Err(if detail.is_empty() {
            "Could not start the Jarvis background service. Check the systemd user-unit logs.".into()
        } else {
            format!("Could not start {unit}: {detail}")
        })
    }
}

#[cfg(target_os = "linux")]
fn local_backend_port(base: &str) -> Option<u16> {
    let Ok(url) = Url::parse(base) else { return None; };
    let Some(host) = url.host_str() else { return None; };
    if !matches!(host, "127.0.0.1" | "localhost" | "::1" | "[::1]") { return None; }
    url.port_or_known_default()
}

#[cfg(target_os = "linux")]
fn local_backend_listening(base: &str) -> bool {
    let Some(port) = local_backend_port(base) else { return false; };
    let host = Url::parse(base).ok().and_then(|url| url.host_str().map(str::to_owned));
    let target = match host.as_deref() {
        Some("::1" | "[::1]") => format!("[::1]:{port}"),
        _ => format!("127.0.0.1:{port}"),
    };
    std::net::TcpStream::connect_timeout(
        &target.parse().expect("valid loopback socket"),
        Duration::from_millis(300),
    ).is_ok()
}

#[cfg(target_os = "linux")]
fn compose_running_services() -> Result<Vec<String>, String> {
    let output = run_bounded(
        &["docker", "compose", "ps", "--status", "running", "--services"],
        Duration::from_secs(4),
    ).map_err(|error| format!("Docker Compose is unavailable: {error}"))?;
    if !output.status.success() {
        let detail = String::from_utf8_lossy(&output.stderr).trim().to_string();
        return Err(if detail.is_empty() {
            "Docker daemon is unavailable. Start and enable Docker Engine, then retry.".into()
        } else {
            format!("Docker daemon or Compose is unavailable: {detail}")
        });
    }
    Ok(String::from_utf8_lossy(&output.stdout)
        .lines()
        .map(str::trim)
        .filter(|service| !service.is_empty())
        .map(str::to_owned)
        .collect())
}

#[tauri::command]
fn start_background_services(state: State<'_, ApiState>, backend: String) -> Result<Value, String> {
    let backend = clean_base(&backend)?;
    *state.base.lock().map_err(|_| "Backend URL unavailable".to_string())? = backend.clone();
    #[cfg(target_os = "linux")]
    {
        let mut errors = Vec::new();
        let mut backend_error = None;
        let mut host_bridge_error = None;
        if local_backend_port(&backend).is_some() && !local_backend_listening(&backend) {
            let mut owns = state.owns_backend_service.lock().map_err(|_| "Service state unavailable")?;
            if !*owns && !unit_active("odysseus-backend.service") {
                match compose_running_services() {
                    Ok(services) if services.iter().any(|service| service == "odysseus") => {}
                    Ok(services) => match start_user_unit("odysseus-backend.service") {
                        Ok(()) => *owns = services.is_empty(),
                        Err(error) => {
                            errors.push(error.clone());
                            backend_error = Some(error);
                        }
                    },
                    Err(error) => {
                        errors.push(error.clone());
                        backend_error = Some(error);
                    }
                }
            }
        }
        if local_backend_port(&backend).is_some() {
            let mut owns = state.owns_bridge_service.lock().map_err(|_| "Service state unavailable")?;
            if !*owns
                && !unit_active("odysseus-host-bridge.service")
                && std::net::TcpStream::connect_timeout(
                    &"127.0.0.1:8096".parse().expect("valid loopback socket"),
                    Duration::from_millis(300),
                ).is_err()
            {
                match start_user_unit("odysseus-host-bridge.service") {
                    Ok(()) => *owns = true,
                    Err(error) => {
                        errors.push(error.clone());
                        host_bridge_error = Some(error);
                    }
                }
            }
        }
        return Ok(json!({
            "requested": true,
            "errors": errors,
            "backend_error": backend_error,
            "host_bridge_error": host_bridge_error,
        }));
    }
    #[cfg(not(target_os = "linux"))]
    {
        let _ = backend;
        Ok(json!({"requested": false, "reason": "services_not_managed_on_this_platform"}))
    }
}

#[tauri::command]
fn background_service_status() -> Value {
    #[cfg(target_os = "linux")]
    {
        let bridge_listener = std::net::TcpStream::connect_timeout(
            &"127.0.0.1:8096".parse().expect("valid loopback socket"),
            Duration::from_millis(300),
        ).is_ok();
        return json!({
            "backend": if unit_active("odysseus-backend.service") {
                "active"
            } else if unit_failed("odysseus-backend.service") {
                "failed"
            } else {
                "inactive"
            },
            "host_bridge": if bridge_listener {
                "listener_reachable"
            } else if unit_active("odysseus-host-bridge.service") {
                "starting"
            } else {
                "inactive"
            },
        });
    }
    #[cfg(not(target_os = "linux"))]
    {
        json!({"backend": "not_managed", "host_bridge": "not_managed"})
    }
}

#[cfg(target_os = "linux")]
fn stop_owned_services(app: &AppHandle) {
    use tauri::Manager;
    let Some(state) = app.try_state::<ApiState>() else { return; };
    if state.owns_bridge_service.lock().is_ok_and(|mut owns| {
        let was_owned = *owns;
        *owns = false;
        was_owned
    }) {
        let _ = run_bounded(
            &["systemctl", "--user", "stop", "--no-block", "odysseus-host-bridge.service"],
            Duration::from_secs(3),
        );
    }
    if state.owns_backend_service.lock().is_ok_and(|mut owns| {
        let was_owned = *owns;
        *owns = false;
        was_owned
    }) {
        let _ = run_bounded(
            &["systemctl", "--user", "stop", "--no-block", "odysseus-backend.service"],
            Duration::from_secs(3),
        );
    }
}

fn main() {
    let initial = build_client().expect("HTTP client initialization failed");
    let builder = tauri::Builder::default()
        .plugin(tauri_plugin_single_instance::init(|app, args, _cwd| {
            if args.iter().any(|arg| arg == "--toggle") {
                toggle_overlay(app);
            } else {
                activate_overlay(app);
            }
        }))
        .plugin(tauri_plugin_global_shortcut::Builder::new().build())
        .manage(ApiState {
            client: Mutex::new(initial),
            base: Mutex::new("http://127.0.0.1:7000".into()),
            shortcut_diagnostic: Mutex::new(String::new()),
            active_shortcut: Mutex::new(if cfg!(target_os = "linux") { "Ctrl+Alt+Shift+J" } else { "Alt+L" }.into()),
            owns_backend_service: Mutex::new(false),
            owns_bridge_service: Mutex::new(false),
            process_started_at: Instant::now(),
        });
    #[cfg(target_os = "linux")]
    let builder = builder.on_permission_request(|webview, kind| {
        let trusted_ui = webview.url().map(|url| is_trusted_microphone_origin(&url)).unwrap_or(false);
        microphone_permission_response(kind, trusted_ui)
    });
    let app = builder
        .setup(|_app| {
            #[cfg(not(target_os = "linux"))]
            if let Some(state) = _app.try_state::<ApiState>() {
                match register_activation_shortcut(_app.handle(), "Alt+L") {
                    Ok(()) => {
                        if let Ok(mut active) = state.active_shortcut.lock() {
                            *active = "Alt+L".into();
                        }
                    }
                    Err(error) => {
                        if let Ok(mut diagnostic) = state.shortcut_diagnostic.lock() {
                            *diagnostic = format!("Alt+L could not be registered: {error}");
                        }
                    }
                }
            }
            Ok(())
        })
        .invoke_handler(tauri::generate_handler![backend_status, login, get_default_chat_model, stt_status, create_voice_session, transcribe, chat, cancel_chat, synthesize, tts_status, get_shortcut_diagnostic, get_activation_shortcut, set_activation_shortcut, start_background_services, background_service_status, desktop_rendered, record_activation_metric, hide_overlay, voice_diagnostic])
        .build(tauri::generate_context!())
        .expect("failed to build Odysseus Voice");
    app.run(|app, event| {
        #[cfg(target_os = "linux")]
        if matches!(event, tauri::RunEvent::Exit) {
            stop_owned_services(app);
        }
    });
}

#[cfg(all(test, target_os = "linux"))]
mod local_backend_tests {
    use super::local_backend_port;

    #[test]
    fn identifies_only_loopback_backend_urls() {
        assert_eq!(local_backend_port("http://127.0.0.1:7000"), Some(7000));
        assert_eq!(local_backend_port("https://localhost"), Some(443));
        assert_eq!(local_backend_port("http://[::1]:8080"), Some(8080));
        assert_eq!(local_backend_port("http://192.168.1.10:7000"), None);
        assert_eq!(local_backend_port("not a url"), None);
    }
}

#[cfg(test)]
mod startup_tests {
    use super::{configured_chat_model, model_stream_failure};
    use serde_json::json;

    #[test]
    fn uses_the_owner_scoped_default_model_without_a_voice_specific_model_name() {
        let value = configured_chat_model(&json!({
            "endpoint_id": "owner-default-endpoint",
            "model": "configured-chat-model",
        }))
        .expect("configured chat model should be accepted");
        assert_eq!(value["endpoint_id"], "owner-default-endpoint");
        assert_eq!(value["model"], "configured-chat-model");
    }

    #[test]
    fn missing_default_endpoint_is_reported_as_a_configuration_error() {
        let error = configured_chat_model(&json!({"endpoint_id": "", "model": ""}))
            .expect_err("missing endpoint should fail explicitly");
        assert!(error.contains("default chat model"));
    }

    #[test]
    fn stream_errors_report_unavailable_model_endpoint_without_raw_payloads() {
        let error = model_stream_failure(
            "event: error\ndata: {\"error\":\"Cannot reach http://host.docker.internal:11434\",\"status\":503}\n\n",
        )
        .expect("error event should fail");
        assert!(error.contains("http://host.docker.internal:11434"));
        assert!(error.contains("configured model service is running"));

        let unsafe_error = model_stream_failure(
            "event: error\ndata: {\"error\":\"Cannot reach https://user:secret@example.test?token=hidden\",\"status\":503}\n\n",
        )
        .expect("error event should fail");
        assert!(!unsafe_error.contains("secret"));
        assert!(!unsafe_error.contains("hidden"));
    }

    #[test]
    fn stream_timeout_and_data_error_events_are_failures() {
        let timeout = model_stream_failure(
            "event: error\ndata: {\"error\":\"Read timeout\",\"status\":504}\n\n",
        )
        .expect("timeout event should fail");
        assert!(timeout.contains("timed out"));

        let data_error = model_stream_failure(
            "data: {\"type\":\"stream_error\",\"status\":502}\n\n",
        )
        .expect("data error event should fail");
        assert!(data_error.contains("HTTP 502"));
    }
}

#[cfg(all(test, target_os = "linux"))]
mod permission_tests {
    use super::{is_trusted_microphone_origin, microphone_permission_response};
    use tauri::webview::{PermissionKind, PermissionResponse};
    use tauri::Url;

    #[test]
    fn allows_only_packaged_and_configured_dev_ui_origins() {
        for allowed in ["tauri://localhost/", "http://localhost:1420/"] {
            assert!(is_trusted_microphone_origin(&Url::parse(allowed).unwrap()), "{allowed}");
        }
    }

    #[test]
    fn rejects_external_and_unconfigured_origins() {
        for denied in [
            "https://example.com/",
            "http://localhost:1421/",
            "http://127.0.0.1:1420/",
            "tauri://example.com/",
        ] {
            assert!(!is_trusted_microphone_origin(&Url::parse(denied).unwrap()), "{denied}");
        }
    }

    #[test]
    fn grants_only_microphone_requests_from_trusted_ui() {
        assert_eq!(
            microphone_permission_response(PermissionKind::Microphone, true),
            PermissionResponse::Allow
        );
        assert_eq!(
            microphone_permission_response(PermissionKind::Microphone, false),
            PermissionResponse::Deny
        );
        assert_eq!(
            microphone_permission_response(PermissionKind::Camera, true),
            PermissionResponse::Deny
        );
    }
}

#[cfg(test)]
mod audio_format_tests {
    use super::audio_mime;

    #[test]
    fn recognizes_tts_wav_and_mp3_payloads_by_signature() {
        assert_eq!(audio_mime(b"RIFF\x24\0\0\0WAVEfmt "), Some("audio/wav"));
        assert_eq!(audio_mime(b"ID3\x04\0\0"), Some("audio/mpeg"));
        assert_eq!(audio_mime(&[0xff, 0xfb, 0x90, 0x64]), Some("audio/mpeg"));
    }

    #[test]
    fn rejects_empty_or_unrecognized_tts_payloads() {
        assert_eq!(audio_mime(&[]), None);
        assert_eq!(audio_mime(b"not audio"), None);
        assert_eq!(audio_mime(b"RIFF"), None);
    }
}
