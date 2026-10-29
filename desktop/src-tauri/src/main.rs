#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use base64::{engine::general_purpose::STANDARD, Engine};
use futures_util::StreamExt;
use reqwest::{cookie::Jar, multipart, Client, Method, Url};
use serde::Serialize;
use serde_json::{json, Value};
use std::{
    fs::{create_dir_all, OpenOptions},
    io::Write,
    sync::{Arc, Mutex},
    time::{Duration, SystemTime, UNIX_EPOCH},
};
use tauri::{Emitter, Manager, State, WebviewWindow};
#[cfg(target_os = "linux")]
use tauri::webview::{PermissionKind, PermissionResponse};

struct ApiState { client: Mutex<Client>, base: Mutex<String>, shortcut_diagnostic: Mutex<String> }

#[derive(Serialize)]
struct TtsAudio { bytes: Vec<u8>, mime_type: String }

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
    if !matches!(category, "webview" | "audio" | "tts") { return; }
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
    let health = request(&state, Method::GET, "/api/health").await?.send().await.map_err(|_| "Odysseus backend is unavailable. Check its address and try again.".to_string())?;
    checked(health).await?;
    let auth = request(&state, Method::GET, "/api/auth/status").await?.send().await.map_err(|e| e.to_string())?;
    let value: Value = checked(auth).await?.json().await.map_err(|_| "Could not read authentication status".to_string())?;
    Ok(Status { healthy: true, authenticated: value.get("authenticated").and_then(Value::as_bool).unwrap_or(false), username: value.get("username").and_then(Value::as_str).map(str::to_string) })
}

#[tauri::command]
async fn login(state: State<'_, ApiState>, backend: String, username: String, password: String, totp_code: Option<String>) -> Result<Value, String> {
    *state.base.lock().map_err(|_| "Backend URL unavailable".to_string())? = clean_base(&backend)?;
    let response = request(&state, Method::POST, "/api/auth/login").await?.json(&json!({"username":username,"password":password,"totp_code":totp_code,"remember":false})).send().await.map_err(|_| "Could not connect to the backend".to_string())?;
    let value: Value = checked(response).await?.json().await.map_err(|_| "Could not read sign-in response".to_string())?;
    Ok(value)
}

#[tauri::command]
async fn get_voice_model(state: State<'_, ApiState>) -> Result<Value, String> {
    let response = request(&state, Method::GET, "/api/models?background=false").await?.send().await.map_err(|_| "Could not load configured models".to_string())?;
    let catalog: Value = checked(response).await?.json().await.map_err(|_| "Could not read model list".to_string())?;
    let items = catalog.get("items").and_then(Value::as_array).ok_or("Backend returned an unexpected model list")?;
    for item in items {
        let models = item.get("models").and_then(Value::as_array).cloned().unwrap_or_default();
        if let Some(model) = models.iter().filter_map(Value::as_str).find(|m| m.to_lowercase().contains("qwen3:8b")) {
            return Ok(json!({"endpoint_id":item.get("endpoint_id"),"model":model}));
        }
    }
    Err("The configured qwen3:8b model was not found. Add or enable it in Odysseus model settings.".into())
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
            let is_error = frame.lines().any(|line| line.trim_end_matches('\r') == "event: error");
            if is_error {
                let _ = app.emit("voice-chat-event", json!({"request_id":request_id,"type":"error","message":"The model could not complete this request"}));
                return Err("The model could not complete this request".into());
            }
            for line in frame.lines().filter(|line| line.starts_with("data: ")) {
                let data = &line[6..];
                if data == "[DONE]" { continue; }
                let Ok(event) = serde_json::from_str::<Value>(data) else { continue };
                if let Some(delta) = event.get("delta").and_then(Value::as_str) {
                    answer.push_str(delta);
                    let _ = app.emit("voice-chat-event", json!({"request_id":request_id,"type":"delta","text":delta}));
                }
                let kind = event.get("type").and_then(Value::as_str).unwrap_or("");
                if kind == "error" || kind == "stream_error" {
                    let _ = app.emit("voice-chat-event", json!({"request_id":request_id,"type":"error","message":"The model could not complete this request"}));
                }
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

fn show_overlay(app: &tauri::AppHandle) {
    if let Some(window) = app.get_webview_window("main") {
        let _ = window.show();
        let _ = window.set_focus();
        let _ = app.emit("voice-activation", ());
    }
}

fn main() {
    let initial = build_client().expect("HTTP client initialization failed");
    let builder = tauri::Builder::default()
        .plugin(tauri_plugin_single_instance::init(|app, _args, _cwd| show_overlay(app)))
        .plugin(tauri_plugin_global_shortcut::Builder::new().build())
        .manage(ApiState { client: Mutex::new(initial), base: Mutex::new("http://127.0.0.1:7000".into()), shortcut_diagnostic: Mutex::new(String::new()) });
    #[cfg(target_os = "linux")]
    let builder = builder.on_permission_request(|webview, kind| {
        let trusted_ui = webview.url().map(|url| is_trusted_microphone_origin(&url)).unwrap_or(false);
        microphone_permission_response(kind, trusted_ui)
    });
    builder
        .setup(|app| {
            #[cfg(not(target_os = "linux"))]
            {
                use tauri_plugin_global_shortcut::{GlobalShortcutExt, ShortcutState};
                let handle = app.handle().clone();
                let result = app.global_shortcut().on_shortcut("Alt+L", move |_app, _shortcut, event| {
                    if event.state == ShortcutState::Pressed { show_overlay(&handle); }
                });
                if let Err(error) = result {
                    if let Some(state) = app.try_state::<ApiState>() {
                        if let Ok(mut diagnostic) = state.shortcut_diagnostic.lock() { *diagnostic = format!("Alt+L could not be registered: {error}"); }
                    }
                }
            }
            #[cfg(target_os = "linux")]
            {
                if let Some(state) = app.try_state::<ApiState>() {
                    if let Ok(mut diagnostic) = state.shortcut_diagnostic.lock() { *diagnostic = "GNOME: configure Alt+L in Keyboard Shortcuts; see desktop/README.md".into(); }
                }
            }
            Ok(())
        })
        .invoke_handler(tauri::generate_handler![backend_status, login, get_voice_model, create_voice_session, transcribe, chat, cancel_chat, synthesize, tts_status, get_shortcut_diagnostic, hide_overlay, voice_diagnostic])
        .run(tauri::generate_context!())
        .expect("failed to run Odysseus Voice");
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
