
# Quick and dirty CLI for poking the /stt endpoint with your actual voice
# instead of curl-ing some wav file. Hit Enter to start recording, hit
# Enter again to stop, and it'll ship the clip off to the server and print
# back whatever whisper thinks you said.

# Needs: pip install sounddevice numpy httpx
# (sounddevice needs the system portaudio lib too -
#  `sudo apt install libportaudio2` on debian/ubuntu,
#  `brew install portaudio` on mac)

# Usage:
#     python voice/test_stt_cli.py
#     python voice/test_stt_cli.py --url http://127.0.0.1:8000/stt
#     python voice/test_stt_cli.py --seconds 5          # fixed-length recording instead
#     python voice/test_stt_cli.py --device 3           # pick a specific input device
#     python voice/test_stt_cli.py --list-devices        # see what mics are available


import argparse
import io
import sys
import threading
import time
import wave

import numpy as np
import sounddevice as sd
import httpx

SAMPLE_RATE = 16000  # whisper wants 16kHz, so just record at that and skip resampling
CHANNELS = 1


def list_devices():
    print(sd.query_devices())


def record_until_enter(device=None) -> np.ndarray:
    
    # Records from the mic until the user hits Enter. Runs the actual
    # recording in a background thread so the main thread is free to just
    # sit there waiting for input().
    
    chunks = []
    stop_flag = threading.Event()

    def callback(indata, frames, time_info, status):
        if status:
            print(f"[mic warning] {status}", file=sys.stderr)
        chunks.append(indata.copy())

    print("Recording... press Enter to stop.")
    stream = sd.InputStream(
        samplerate=SAMPLE_RATE,
        channels=CHANNELS,
        dtype="int16",
        device=device,
        callback=callback,
    )
    with stream:
        input()  # blocks here until Enter, meanwhile callback keeps filling `chunks`

    if not chunks:
        return np.zeros((0,), dtype="int16")
    return np.concatenate(chunks, axis=0)


def record_fixed(seconds: float, device=None) -> np.ndarray:
    print(f"Recording for {seconds}s...")
    audio = sd.rec(
        int(seconds * SAMPLE_RATE),
        samplerate=SAMPLE_RATE,
        channels=CHANNELS,
        dtype="int16",
        device=device,
    )
    sd.wait()
    return audio


def to_wav_bytes(audio: np.ndarray) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(CHANNELS)
        w.setsampwidth(2)  # int16 = 2 bytes
        w.setframerate(SAMPLE_RATE)
        w.writeframes(audio.tobytes())
    return buf.getvalue()


def send_to_stt(wav_bytes: bytes, url: str) -> dict:
    files = {"audio": ("clip.wav", wav_bytes, "audio/wav")}
    r = httpx.post(url, files=files, timeout=120)
    r.raise_for_status()
    return r.json()


def main():
    parser = argparse.ArgumentParser(description="Mic -> /stt test CLI")
    parser.add_argument("--url", default="http://127.0.0.1:8000/voice/stt", help="STT endpoint URL")
    parser.add_argument("--seconds", type=float, default=None,
                         help="Record a fixed length instead of press-Enter-to-stop")
    parser.add_argument("--device", type=int, default=None, help="Input device index")
    parser.add_argument("--list-devices", action="store_true", help="List audio devices and exit")
    parser.add_argument("--save", type=str, default=None, help="Also save the recording to this wav path")
    parser.add_argument("--loop", action="store_true", help="Keep recording/transcribing until Ctrl+C")
    args = parser.parse_args()

    if args.list_devices:
        list_devices()
        return

    try:
        while True:
            if args.seconds:
                audio = record_fixed(args.seconds, device=args.device)
            else:
                audio = record_until_enter(device=args.device)

            if audio.size == 0:
                print("Got no audio, skipping.")
            else:
                wav_bytes = to_wav_bytes(audio)

                if args.save:
                    with open(args.save, "wb") as f:
                        f.write(wav_bytes)
                    print(f"Saved recording to {args.save}")

                print(f"Sending {len(wav_bytes)} bytes to {args.url} ...")
                t0 = time.time()
                try:
                    result = send_to_stt(wav_bytes, args.url)
                except httpx.HTTPStatusError as e:
                    print(f"Server error: {e.response.status_code} {e.response.text}")
                    if not args.loop:
                        return
                    continue
                except httpx.RequestError as e:
                    print(f"Couldn't reach the server: {e}")
                    if not args.loop:
                        return
                    continue

                dt = time.time() - t0
                print(f"\n--- Transcript ({dt:.2f}s round trip, language={result.get('language')}) ---")
                print(result.get("text", "").strip() or "(empty)")
                print("---\n")

            if not args.loop:
                break

            print("Press Enter to record again, or Ctrl+C to quit.")
            input()

    except KeyboardInterrupt:
        print("\nBye.")


if __name__ == "__main__":
    main()
