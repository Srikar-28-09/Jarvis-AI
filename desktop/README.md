# Odysseus Voice desktop overlay

This is a separate Tauri v2 desktop client. It does not start or modify the Odysseus backend. The native HTTP client owns the backend's HttpOnly session cookie in memory; credentials and bearer tokens are not passed to the UI or written to disk. The default local backend is `http://127.0.0.1:7000`, and the sign-in form can use another existing address.

This guide describes the current implementation. Build, permissions, and desktop behavior still require validation on each target platform as described below.

## Ubuntu GNOME on Wayland

Configure the shortcut through GNOME's Custom Shortcuts settings below. Window focus and always-on-top behavior are best effort under Wayland; Mutter may deny focus or stacking requests while another app is active.

### Prerequisites

Tauri's Ubuntu prerequisites are:

```sh
sudo apt install build-essential curl wget file libxdo-dev libssl-dev librsvg2-dev libwebkit2gtk-4.1-dev
```

Install the Rust toolchain with rustup and the Node.js dependencies. Tauri uses the platform native webview: WebKitGTK on Linux, WebView2 on Windows, and WKWebView on macOS.

```sh
cd <path-to-odysseus>/desktop
npm ci
npm run tauri dev
```

For a release binary use `npm run tauri build`. The development and release app both connect to the already-running Odysseus backend. After sign-in, the overlay looks up the configured `qwen3:8b` model. STT must be enabled in Odysseus settings with its local Whisper or endpoint provider; browser-only STT is not used by this client. Before each answer the client checks `/api/tts/stats`, then requests audio from `/api/tts/synthesize`, which uses the backend's configured provider, model, voice, cache, and speed.

### Configure Alt+L

1. Build the release binary.
2. Open **Settings → Keyboard → View and Customize Shortcuts → Custom Shortcuts**.
3. Add a shortcut named **Odysseus Voice** with command:

   ```text
   <path-to-odysseus>/desktop/bin/odysseus-voice --toggle
   ```

4. Assign **Alt+L**. If GNOME reports a conflict, choose another key or remove that conflicting assignment.

The Tauri single-instance plugin is configured to forward later invocations to the existing overlay. The command is intended to start the overlay if it is not already running. Escape hides the window while leaving the app process and backend running. The Linux client does not install a keyboard hook or claim direct portal registration.

The native webview requests microphone access on activation. If permission is denied, allow it in system settings and reopen the overlay, or type into the message field. Speech endpointing uses webview microphone level with a 1.25 second quiet period and a 15 second utterance cap. Capture is configured to stop during Jarvis playback; reliable full-duplex barge-in is not available.

## Windows and macOS

Tauri uses WebView2 on Windows and WKWebView on macOS. The client registers Alt+L with the native global-shortcut plugin on those targets, and its single-instance behavior focuses the running overlay. Windows requires the standard Tauri C++ build tools and WebView2 runtime; macOS requires Xcode Command Line Tools. These platforms and their native shortcuts require validation on their target operating systems. The existing Odysseus STT adapter uploads WebM audio, so voice capture requires WebM MediaRecorder support; if a platform webview does not provide it, typed input remains available.

## Tests and verification limits

Run the state-machine tests with `npm test` after installing dependencies. Native builds, portal state, microphone permission flow, WebKitGTK MediaRecorder support, Jarvis playback, Wayland stacking/focus, and consecutive live audio turns require validation on the target desktop.
