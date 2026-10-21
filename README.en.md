# ASR Bridge

A Windows desktop application and a local OpenAI-compatible audio transcription API for three providers. Audio is sent to the selected provider, not to OpenAI.

[中文说明](README.md) · [Portable usage](docs/PORTABLE.md) · [API reference](docs/api.html)

## Models and requirements

| model | Provider | Requirements |
|---|---|---|
| `wechat-voice` | WeChat desktop | Logged-in WeChat with native speech capability; reference build 4.1.13.65 |
| `wetype-voice` | WeType | WeType running locally; 3.0.0.34 and 3.0.0.37 verified |
| `doubao-voice` | Doubao web | Normal anonymous visitor initialization and access to its audio service |

No browser credentials are read for Doubao. Version and fingerprint differences produce warnings and do not block calls. WeType encoder addresses are resolved from function signatures. Warnings appear in JSON `warnings`, the `x-asr-warnings` header, `/health` and the desktop diagnostics viewer. Actual call failures include their message, code, underlying details and request ID. Providers need not all be available at startup; failure of one route does not disable others. Client upgrades or private protocol changes may require new adaptations.

## Desktop application

Extract the entire `WeChatASRBridge-0.3.3-windows-x64-portable.zip` and run the executable. Keep `_internal` beside it. A local build is written to `dist/`; the latest published GitHub release may precede the source version.

The **Home** tab shows service status, independent concurrency pools, Base URL, model identifiers and a documentation button. Green squares indicate occupied slots. The **Manual transcription** tab provides model selection, file/sample selection and result copying/saving.

Settings include port, an explicit authentication switch, local API key and positive capacities for each provider. Each model defaults to 4 concurrent requests, and each provider is scheduled independently. Requests exceeding its configured capacity queue. Existing saved capacities are preserved. These settings do not guarantee upstream throughput. Large pools paginate their slot display.

Closing the window hides it to the system tray. Normal exit waits for requests and safe native cleanup, and stops only application-owned services. Settings persist; API keys use current-user Windows DPAPI encryption. Native ownership uncertainty retains the agent instead of unloading a potentially live callback.

## Source and API usage

Use Windows x64 and a Python 3.11 conda environment:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\setup.ps1
.\env\python.exe desktop.py
```

For an HTTP-only unified service:

```powershell
.\env\python.exe server.py --port 8765 --wechat-concurrency 4 --wetype-concurrency 4 --doubao-concurrency 4
```

Use `POST /v1/audio/transcriptions` with multipart `file` and `model`, and optional `response_format=json|text`. OpenAI SDK base URL is `http://127.0.0.1:8765/v1`. When authentication is disabled, arbitrary API keys and missing Authorization headers are ignored. OpenAI SDKs may still require a nonempty placeholder. Entering a key in settings does not enable authentication; turn on the explicit switch to require the configured key. Existing protected installations retain authentication during migration.

`GET /v1/models` lists model identifiers, `/health` returns pool status without submitting audio, and `/docs` serves the bundled HTML reference. The desktop can also open the reference offline.

Files are limited to 25 MiB. FFmpeg normalizes complete inputs in memory. Only final JSON/text responses are implemented. `stream=false` and `temperature=0` are compatibility defaults. Unsupported features, including `language`, `prompt`, subtitles and timestamps, return explicit errors.

The service binds to loopback by default. Non-loopback binding requires `WECHAT_API_KEY`. This project contains no vendor executables, account credentials or private recordings.

## Verification and build

```powershell
.\env\python.exe -m unittest discover -s tests -p "test_*.py" -v
powershell -NoProfile -ExecutionPolicy Bypass -File .\build.ps1
```

Offline tests cover independent routing/queues, independent per-model budgets, queue cancellation, callback ownership, encrypted settings and native DPI layout. These do not prove equivalent live upstream throughput. The packaged executable also supports `--self-test` and `--self-test-lifecycle`.

See [v0.3.3 verification](docs/VERSION_WARNING_VERIFICATION.md) for the current validation scope and [v0.3.2 verification](docs/UI_CONCURRENCY_VERIFICATION.md) for earlier checks. `tests/verify_unified_live.py` is opt-in and sends only repository-synthesized audio to selected providers. Long-term reliability, arbitrary concurrent load and future client versions remain unverified. The project is unaffiliated with Tencent, ByteDance and OpenAI.
