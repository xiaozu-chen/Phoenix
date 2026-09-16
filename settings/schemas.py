from pydantic import BaseModel


class ChatIn(BaseModel):
    message: str


class TTSIn(BaseModel):
    text: str
    speaker_id: str | None = None
    speed: float | None = None
    sdp_ratio: float | None = None
    noise_scale: float | None = None
    noise_scale_w: float | None = None
    use_clause_prosody: bool | None = None


class InterruptIn(BaseModel):
    # speech_id comes back from /voice/tts as the X-Speech-Id header.
    speech_id: str
    # Wall-clock seconds of *this* speech's audio that had actually
    # played back on the client when VAD crossed the barge-in threshold
    # (see routes/voice.py's /interrupt docstring for why VAD, not STT,
    # is what should be calling this).
    elapsed_seconds: float