import os
import sys
from pathlib import Path
from dataclasses import dataclass

from dotenv import load_dotenv


def _load_env() -> None:
    candidates = [Path.cwd() / ".env"]
    if getattr(sys, "frozen", False):
        candidates.append(Path(sys.executable).resolve().parent / ".env")
        if hasattr(sys, "_MEIPASS"):
            candidates.append(Path(sys._MEIPASS) / ".env")
    candidates.append(Path(__file__).resolve().parent.parent / ".env")

    for path in candidates:
        if path.exists():
            load_dotenv(path, override=False)


_load_env()


def _base_dir() -> Path:
    """Directory that db/log/tmp paths are resolved against.

    NOT the same problem as _load_env() above -- that only needs to find
    an .env file that may or may not exist. This needs a directory the
    process can always *write* into.

    In dev, that's the project root (stable, always writable, matches
    what everyone's used to seeing in ./data/...).

    Frozen, it must NOT be Path(sys.executable).parent: on Windows the
    exe commonly lives in Program Files / a Desktop shortcut target /
    wherever the installer put it, which regular (non-admin) users can't
    write to -- and the process's cwd at launch is whatever the OS or
    shortcut set it to, not necessarily that folder either. Relative
    paths like "data/logs/session.log" silently resolved against
    Path.cwd() is exactly what broke this: it worked in dev only because
    dev always happens to launch with cwd == project root.

    Use the OS's actual per-user app-data directory instead, which is
    guaranteed writable and stable across launches regardless of where
    the binary sits or what cwd it's given.
    """
    if not getattr(sys, "frozen", False):
        return Path(__file__).resolve().parent.parent

    if sys.platform == "win32":
        root = os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local"
    elif sys.platform == "darwin":
        root = Path.home() / "Library" / "Application Support"
    else:
        root = os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share"

    base = Path(root) / "Phoenix"
    base.mkdir(parents=True, exist_ok=True)
    return base


BASE_DIR = _base_dir()


def _p(relative: str) -> str:
    """Resolve one of the historically-relative "data/..." paths against
    BASE_DIR instead of leaving it relative (i.e. cwd-dependent)."""
    return str(BASE_DIR / relative)

@dataclass
class Config:
    llama_url: str                  = "http://127.0.0.1:8080"
    db_path: str                    = _p("data/database/phoenix.db")
    data_path: str                  = _p("data/misc/data.jsonl")
    context_window: int             = 12
    memory_topk: int                = 5
    recall_scan_limit: int          = 2000
    timestamp_gap_seconds: int      = 1800
    interruption_history_limit: int = 6
    prompt_path: str                = _p("data/system_prompt.txt")
    extensions_dir: str             = _p("data/extensions")
    persona: str                    = """
    
    """
    
@dataclass
class Logging:
    session: str                    = "data/logs/session"
    session_path: str               = _p("data/logs/session.log")
    
    system: str                     = "data/logs/system"
    system_path: str                = _p("data/logs/system.log")
    
@dataclass
class VoiceLifecycleConfig:
    # Manual wake is always available regardless of these -- this is just
    # the auto-sleep side.
    idle_timeout_seconds: int       = 600   # 10 minutes
    idle_check_interval_seconds: int = 30
    # If True, background-load (not blocking startup) both voice models
    # right after the server comes up, so the first real request doesn't
    # eat the load latency. Set False to start fully asleep and rely on
    # /voice/wake or the first request to load on demand.
    warm_on_startup: bool           = True


@dataclass
class LLMConfig:
    model_path: str                 = os.environ.get("LLAMA_MODEL_PATH", "")
    idle_timeout_seconds: int       = int(os.environ.get("LLAMA_IDLE_TIMEOUT_SECONDS", "600"))
    idle_check_interval_seconds: int = int(os.environ.get("LLAMA_IDLE_CHECK_INTERVAL_SECONDS", "30"))
    warm_on_startup: bool           = os.environ.get("LLAMA_WARM_ON_STARTUP", "true").lower() in ("1", "true", "yes", "on")
    startup_timeout_seconds: float  = float(os.environ.get("LLAMA_STARTUP_TIMEOUT_SECONDS", "120"))
    startup_poll_seconds: float     = float(os.environ.get("LLAMA_STARTUP_POLL_SECONDS", "0.5"))
    shutdown_timeout_seconds: float = float(os.environ.get("LLAMA_SHUTDOWN_TIMEOUT_SECONDS", "10"))
    log_path: str                   = _p("data/logs/llama-server.log")


@dataclass
class STTConfig:
    model_size: str                 = "small"
    device: str                     = "cuda"
    compute_type: str               = "float16"
    model_dir: str                  = os.environ.get("STT_MODEL_DIR", "")
    language: str                   = "en"
    tmp_dir: str                    = _p("data/misc/voice_tmp")
    
@dataclass
class TTSConfig:
    # "turbo" (GPT2-medium T3 backbone) or "nano" (GPT2-small backbone) --
    # both are the same ChatterboxTurboTTS class, this just controls the
    # `nano=` flag passed to from_local()/from_pretrained() and which
    # checkpoint folder/HF repo we default to. See get_model() in
    # voice/tts/main.py.
    variant: str                    = os.environ.get("CHATTERBOX_VARIANT", "turbo").strip().lower()
    model_dir: str                  = os.environ.get(
        "CHATTERBOX_MODEL_DIR",
        str(
            (Path(getattr(sys, "_MEIPASS")) / "models" / f"chatterbox-{os.environ.get('CHATTERBOX_VARIANT', 'turbo').strip().lower()}")
            if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS")
            else Path(__file__).resolve().parents[2] / "m" / "vm" / "tts" / f"chatterbox-{os.environ.get('CHATTERBOX_VARIANT', 'turbo').strip().lower()}"
        ),
    )
    source_dir: str                 = os.environ.get(
        "CHATTERBOX_SOURCE_DIR",
        # ChatterBox is vendored directly under voice/tts/chatterbox (the
        # same spot MeloTTS used to live), so this just needs to point at
        # voice/tts -- _ensure_chatterbox_source() then treats the
        # "chatterbox" subfolder there as the package. Frozen builds get it
        # from the PyInstaller bundle root instead (see main.spec).
        str(Path(getattr(sys, "_MEIPASS")) if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS") else Path(__file__).resolve().parents[1] / "voice" / "tts"),
    )
    speaker_id: str                 = "phoenix"
    device: str                     = "cuda"
    speed: float                    = 1.0
    tmp_dir: str                    = _p("data/misc/tts_tmp")
    repetition_penalty: float       = float(os.environ.get("CHATTERBOX_REPETITION_PENALTY", "1.2"))
    top_p: float                    = float(os.environ.get("CHATTERBOX_TOP_P", "0.95"))
    temperature: float              = float(os.environ.get("CHATTERBOX_TEMPERATURE", "1"))
    top_k: int                      = int(os.environ.get("CHATTERBOX_TOP_K", "1000"))
    # Flow-matching steps in S3Gen's vocoder. Lower = faster, at some
    # audio-quality cost (more "smeared"/buzzy). 2 is ChatterBox's own
    # default and already fairly aggressive; 1 saves a bit more time if
    # you're desperate for speed and can tolerate the quality hit.
    n_cfm_timesteps: int             = int(os.environ.get("CHATTERBOX_CFM_TIMESTEPS", "2"))
    audio_prompt_path: str          = os.environ.get("CHATTERBOX_AUDIO_PROMPT_PATH", "")
    hf_home: str                    = os.environ.get("HF_HOME", _p("data/misc/huggingface"))
    # Runs generation under torch.autocast(fp16) on CUDA instead of full
    # fp32 -- can meaningfully speed up inference on GPUs with fp16
    # tensor cores, at some risk of subtly different (occasionally worse)
    # audio quality. Off by default since it's a real quality tradeoff,
    # not just a free win -- A/B it against fp32 before leaving it on.
    amp: bool                        = os.environ.get("CHATTERBOX_AMP", "false").lower() in ("1", "true", "yes", "on")
    norm_loudness: bool             = os.environ.get("CHATTERBOX_NORM_LOUDNESS", "true").lower() in ("1", "true", "yes", "on")
    # No longer used by voice/tts/main.py (generation isn't segmented into
    # clauses anymore -- see synthesize_speech's docstring), kept only so
    # TTSIn's field and any existing client requests don't break.
    use_clause_prosody: bool        = False
    voices_dir: str = _p("data/misc/tts_voices")