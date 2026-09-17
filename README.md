# Phoenix

Phoenix is a local, voice-enabled AI assistant for Windows. It provides a
browser UI and a FastAPI backend for:

- Streaming chat backed by a local `llama-server` model
- Long-term conversation memory stored in SQLite and JSONL
- Speech-to-text with faster-whisper
- Text-to-speech with ChatterBox Turbo or Nano
- Voice wake/sleep lifecycle management
- A Windows system-tray application
- Hot-loadable extensions that communicate over a JSON-lines protocol

The application is designed to run locally. Model inference, conversation
history, logs, and generated audio remain on the machine unless an extension
or an explicitly configured external service sends them elsewhere.

## Requirements

- Windows 10 or later
- Python 3.11 recommended
- An NVIDIA GPU with a compatible CUDA runtime is recommended for the default
  voice configuration (`cuda`, `float16`)
- A local GGUF language model for `llama-server`
- A ChatterBox checkpoint (`turbo` or `nano`)
- A microphone and speakers for voice features

CPU inference is possible only where the individual model/backend supports it;
the default configuration assumes CUDA.

## Installation

Clone the repository and create or activate a virtual environment:

```powershell
git clone https://github.com/xiaozu-chen/Phoenix.git
cd Phoenix
python -m venv venv
.\venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt
```

PowerShell may require an execution-policy adjustment before activating a
local environment:

```powershell
Set-ExecutionPolicy -Scope Process Bypass
```

The repository includes the `bin\llama` runtime and ChatterBox source. Model
weights are large and should be supplied separately when they are not already
present in the checkout.

## Configuration

Create a `.env` file in the repository root. At minimum, configure a GGUF
model for the local llama server:

```dotenv
LLAMA_MODEL_PATH=C:\models\your-model.gguf
```

Useful optional settings include:

```dotenv
# Backend listener
BACKEND_HOST=127.0.0.1
BACKEND_PORT=8000
BACKEND_LOG_LEVEL=info

# LLM lifecycle
LLAMA_WARM_ON_STARTUP=true
LLAMA_IDLE_TIMEOUT_SECONDS=600
LLAMA_STARTUP_TIMEOUT_SECONDS=120

# Voice model selection
CHATTERBOX_VARIANT=turbo
CHATTERBOX_MODEL_DIR=C:\models\chatterbox-turbo
STT_MODEL_DIR=C:\models\faster-whisper

# Optional voice tuning
CHATTERBOX_AUDIO_PROMPT_PATH=C:\models\voices\phoenix.wav
CHATTERBOX_TEMPERATURE=1
CHATTERBOX_TOP_P=0.95
CHATTERBOX_CFM_TIMESTEPS=2
```

`CHATTERBOX_VARIANT` accepts `turbo` or `nano`. If
`CHATTERBOX_MODEL_DIR` is not set, the application resolves the model from its
configured local model directory. The first launch can also open the desktop
setup wizard, which saves user settings under `data\`.

## Running from source

Start the backend and tray application with:

```powershell
python phoenix.py
```

The API listens on `http://127.0.0.1:8000` by default. If the built frontend
exists in `static\`, the same process serves it at the root URL:

```text
http://127.0.0.1:8000/
```

The backend starts the local `llama-server` on port `8080` when needed. An
already-running compatible server at the configured llama URL is treated as an
external server and is not stopped by Phoenix.

If `static\` is absent or incomplete, the API still starts, but the browser UI
is not served. Set `FRONTEND_DIST_DIR` to point at a built frontend directory
when using a frontend build outside the repository.

## API quick reference

### Chat

Submit a message. The response is a server-sent event stream:

```powershell
curl.exe -N -X POST http://127.0.0.1:8000/chat `
  -H "Content-Type: application/json" `
  -d '{"message":"Hello, Phoenix"}'
```

Related endpoints:

| Method | Endpoint | Description |
| --- | --- | --- |
| `POST` | `/chat` | Stream a chat response |
| `GET` | `/history` | Return stored messages |
| `POST` | `/reset` | Delete stored conversation messages |

### Voice

Check model state and explicitly wake the LLM and voice models:

```powershell
curl.exe http://127.0.0.1:8000/voice/status
curl.exe -X POST http://127.0.0.1:8000/voice/wake
```

Transcribe an audio file:

```powershell
curl.exe -X POST http://127.0.0.1:8000/voice/stt `
  -F "audio=@samples\Phoenix.WAV"
```

Synthesize speech. The response body is WAV audio and includes an
`X-Speech-Id` header used by interruption handling:

```powershell
curl.exe -X POST http://127.0.0.1:8000/voice/tts `
  -H "Content-Type: application/json" `
  -d '{"text":"Hello from Phoenix"}' `
  --output reply.wav
```

Voice endpoints:

| Method | Endpoint | Description |
| --- | --- | --- |
| `GET` | `/voice/status` | Report voice and LLM lifecycle state |
| `POST` | `/voice/wake` | Load the voice models and LLM |
| `POST` | `/voice/warmup` | Run a short STT/TTS readiness check |
| `POST` | `/voice/stt` | Transcribe an uploaded audio file |
| `POST` | `/voice/tts` | Generate WAV audio from text |
| `POST` | `/voice/interrupt` | Register a playback interruption |

## Memory and local data

Runtime data is stored under `data\` in a source checkout:

- `data\database\phoenix.db` — conversation and embedding storage
- `data\misc\data.jsonl` — append-only conversation turn export
- `data\system_prompt.txt` — editable system prompt
- `data\user_settings.json` — persisted persona/model settings
- `data\logs\` — session, system, and llama-server logs
- `data\extensions\` — imported extensions
- `data\misc\voice_tmp\` and `data\misc\tts_tmp\` — temporary audio files

For a frozen build, writable data is moved to the operating system's
per-user application-data directory (on Windows:
`%LOCALAPPDATA%\Phoenix`).

## Extensions

Extensions are long-running subprocesses discovered from `data\extensions\`
or added by path in the configuration UI. Each extension contains an
`extension.json` manifest and an executable (or development Python script):

```text
weather/
  extension.json
  weather.exe
```

Example manifest:

```json
{
  "id": "weather",
  "name": "Weather",
  "version": "1.0.0",
  "description": "Looks up current weather.",
  "entry": "weather.exe",
  "interpreter": null,
  "args": []
}
```

The backend sends one JSON object per line on stdin and expects one response
per line on stdout. The request `id` must be echoed in the response:

```json
{"id":"request-id","ok":true,"result":{"value":"sunny"}}
```

Keep stdout reserved for protocol messages; diagnostic output belongs on
stderr. See [`extensions/PROTOCOL.md`](extensions/PROTOCOL.md) for the full
handshake, error, timeout, and lifecycle rules.

## Precomputing a voice

To avoid re-encoding the same reference clip on every synthesis request, bake
voice conditionals into a checkpoint:

```powershell
python precompute.py `
  --model-dir C:\models\chatterbox-turbo `
  --ref-wav samples\Phoenix.WAV `
  --out C:\models\chatterbox-turbo\conds.pt `
  --backup
```

Use a clean, single-speaker reference clip longer than five seconds. To keep
the checkpoint unchanged, write the result to
`data\misc\tts_voices\<speaker_id>.pt` instead and select that speaker in the
client.

## Tests and diagnostics

Run the repository's tests from the activated environment:

```powershell
pytest
```

The voice test helpers can be used after the server is running. For example,
the STT helper records from a microphone and sends the clip to
`/voice/stt`:

```powershell
python voice\test_stt_cli.py --seconds 5
python voice\test_stt_cli.py --list-devices
```

When diagnosing startup or inference issues, inspect:

- `data\logs\system.log`
- `data\logs\session.log`
- `data\logs\llama-server.log`

Common causes of failed startup are a missing `LLAMA_MODEL_PATH`, missing
ChatterBox weights, an incompatible CUDA installation, or a frontend directory
that has not been built/copied into `static\`.

## Building a Windows distribution

The PyInstaller spec produces an onedir distribution named `PhoenixSvc`.
Before building, make sure the selected ChatterBox checkpoint is present under
`models\chatterbox-turbo` or `models\chatterbox-nano`, and that the static
frontend files are in `static\`:

```powershell
pyinstaller main.spec
```

The resulting application is placed under `dist\PhoenixSvc\`. The onedir
layout is intentional: it avoids extracting the large model/runtime payload
to a temporary directory on every launch.

## License

No license file is currently included in this repository. Add or document a
project license before distributing Phoenix outside the project.
