# Quick and dirty CLI for poking the /tts endpoint. Type some text (or pass
# it with --text / --loop), it ships it off to the server, saves the wav,
# and optionally plays it back right away so you can hear what Mint-sama
# sounds like without digging through a file explorer.

# Needs: pip install sounddevice numpy httpx
# (sounddevice needs the system portaudio lib too -
#  `sudo apt install libportaudio2` on debian/ubuntu,
#  `brew install portaudio` on mac, nothing extra needed on windows)

# Usage:
#     python voice/test_tts_cli.py
#     python voice/test_tts_cli.py --text "Hey, this is a test."
#     python voice/test_tts_cli.py --url http://127.0.0.1:8000/tts
#     python voice/test_tts_cli.py --speaker-id EN-BR --speed 1.1
#     python voice/test_tts_cli.py --save out.wav --no-play
#     python voice/test_tts_cli.py --loop
#
# Tier 1/2 prosody A/B testing:
#     python voice/test_tts_cli.py --text "Wait, really?!" --sdp-ratio 0.8 --noise-scale 0.9
#     python voice/test_tts_cli.py --text "Wait, really?!" --no-clause-prosody   # tier 1 only, for comparison
#     python voice/test_tts_cli.py --loop --sdp-ratio 0.75   # keep one override fixed across a whole session


import argparse
import io
import shutil
import sys
import threading
import time
import wave

import numpy as np
import sounddevice as sd
import httpx


def send_to_tts(
    text: str,
    url: str,
    speaker_id: str | None,
    speed: float | None,
    sdp_ratio: float | None,
    noise_scale: float | None,
    noise_scale_w: float | None,
    use_clause_prosody: bool | None,
) -> bytes:
    payload = {"text": text}
    if speaker_id:
        payload["speaker_id"] = speaker_id
    if speed is not None:
        payload["speed"] = speed
    # Tier 1/2 knobs -- only sent if explicitly passed, so the server's
    # config.py defaults are used otherwise.
    if sdp_ratio is not None:
        payload["sdp_ratio"] = sdp_ratio
    if noise_scale is not None:
        payload["noise_scale"] = noise_scale
    if noise_scale_w is not None:
        payload["noise_scale_w"] = noise_scale_w
    if use_clause_prosody is not None:
        payload["use_clause_prosody"] = use_clause_prosody

    r = httpx.post(url, json=payload, timeout=120)
    r.raise_for_status()
    return r.content


def estimate_word_timings(text: str, duration: float) -> list[tuple[str, float]]:
    
    # The /tts endpoint only gives us back a wav, no phoneme/word-level
    # timing, so there's no way to sync captions exactly. This just
    # splits the text into words and hands each one a slice of the
    # total duration proportional to its length (+1 so short words
    # still get a beat). It's a rough approximation, not a
    # frame-accurate sync, but it tracks the audio well enough to be
    # readable.
    
    words = text.split()
    if not words:
        return []
    weights = [len(w) + 1 for w in words]
    total_weight = sum(weights)
    return [(w, duration * wt / total_weight) for w, wt in zip(words, weights)]


def _rows_for(line: str, term_width: int) -> int:
    # How many terminal rows a line of this length wraps into once the
    # terminal auto-wraps it. Empty line still occupies 1 row.
    return max(1, -(-len(line) // term_width))  # ceil division


def print_captions(text: str, duration: float):
    
    # Typewriter-style live captions: builds the line up one word at a
    # time, timed to roughly track the audio. Meant to be run in its
    # own thread alongside sd.play() so it doesn't block playback.
    #
    # If the line is short enough to fit on one terminal row, a plain
    # "\r + clear to end of line" redraw works fine. But once the
    # growing caption wraps onto 2+ rows, \r only jumps to the start of
    # the CURRENT row and \033[K only clears that row -- the earlier
    # wrapped rows never get cleared, so old text piles up underneath
    # the new text on every redraw. To fix that we track how many rows
    # the previous redraw wrapped into, move the cursor back up to the
    # top of that block, then clear everything below it (\033[J) before
    # writing the new line.
    #
    # None of this means anything when stdout isn't a real terminal
    # (piped/redirected output, some CI logs, certain Windows setups),
    # so in that case we skip the cursor tricks entirely and just
    # stream words out plainly, letting them scroll normally.
    
    interactive = sys.stdout.isatty()
    term_width = shutil.get_terminal_size(fallback=(80, 24)).columns

    line = ""
    prev_rows = 0
    for word, dt in estimate_word_timings(text, duration):
        line += (" " if line else "") + word

        if interactive:
            if prev_rows > 1:
                sys.stdout.write(f"\033[{prev_rows - 1}A")  # cursor up to top of block
            sys.stdout.write("\r\033[J")  # back to col 0, clear everything below
            sys.stdout.write(line)
            prev_rows = _rows_for(line, term_width)
        else:
            sys.stdout.write(word + " ")

        sys.stdout.flush()
        time.sleep(dt)
    sys.stdout.write("\n")


def play_wav_bytes(wav_bytes: bytes, text: str | None = None, captions: bool = True):
    
    # Reads the wav back out of memory and plays it through the default
    # output device. No temp file needed, sounddevice is happy with a
    # plain numpy array. If captions is on and we have the source text,
    # prints it out typewriter-style in a background thread synced to
    # the audio's duration.
    
    buf = io.BytesIO(wav_bytes)
    with wave.open(buf, "rb") as w:
        channels = w.getnchannels()
        sample_rate = w.getframerate()
        sampwidth = w.getsampwidth()
        n_frames = w.getnframes()
        frames = w.readframes(n_frames)

    dtype = {1: "uint8", 2: "int16", 4: "int32"}.get(sampwidth, "int16")
    audio = np.frombuffer(frames, dtype=dtype)
    if channels > 1:
        audio = audio.reshape(-1, channels)

    duration = n_frames / float(sample_rate)

    print("Playing...")
    sd.play(audio, samplerate=sample_rate)  # non-blocking, returns immediately

    if captions and text:
        cap_thread = threading.Thread(
            target=print_captions, args=(text, duration), daemon=True
        )
        cap_thread.start()
        sd.wait()
        cap_thread.join()
    else:
        sd.wait()


def main():
    parser = argparse.ArgumentParser(description="Text -> /tts test CLI")
    parser.add_argument("--url", default="http://127.0.0.1:8000/voice/tts", help="TTS endpoint URL")
    parser.add_argument("--text", type=str, default=None,
                         help="Text to speak. If omitted, you'll be prompted interactively")
    parser.add_argument("--speaker-id", type=str, default=None,
                         help="Override the server's default speaker (e.g. EN-US, EN-BR)")
    parser.add_argument("--speed", type=float, default=None,
                         help="Override the server's default playback speed (1.0 = normal)")
    parser.add_argument("--sdp-ratio", type=float, default=None,
                         help="Tier 1/2: stochastic vs deterministic duration predictor weight "
                              "(server default ~0.65; keep under ~0.9 or rhythm gets slurred/unstable)")
    parser.add_argument("--noise-scale", type=float, default=None,
                         help="Tier 1/2: flow-decoder latent variance, i.e. pitch/timbre variation "
                              "(server default ~0.75; keep under ~1.1-1.2)")
    parser.add_argument("--noise-scale-w", type=float, default=None,
                         help="Tier 1/2: stochastic duration predictor's own variance, "
                              "i.e. per-phoneme duration variation (server default ~1.0)")
    parser.add_argument("--no-clause-prosody", action="store_true",
                         help="Disable tier 2 (punctuation-driven per-clause nudging) for this "
                              "request, so you can A/B it against tier-1-only")
    parser.add_argument("--save", type=str, default="tts_test_output.wav",
                         help="Where to save the returned wav (default: tts_test_output.wav)")
    parser.add_argument("--no-play", action="store_true", help="Don't play the audio back, just save it")
    parser.add_argument("--no-captions", action="store_true",
                         help="Don't print live captions while audio plays")
    parser.add_argument("--loop", action="store_true", help="Keep prompting for text until Ctrl+C")
    args = parser.parse_args()

    use_clause_prosody = False if args.no_clause_prosody else None

    try:
        while True:
            text = args.text or input("Text to speak: ").strip()

            if not text:
                print("Empty text, skipping.")
            else:
                print(f"Sending {len(text)} chars to {args.url} ...")
                t0 = time.time()
                try:
                    wav_bytes = send_to_tts(
                        text,
                        args.url,
                        args.speaker_id,
                        args.speed,
                        args.sdp_ratio,
                        args.noise_scale,
                        args.noise_scale_w,
                        use_clause_prosody,
                    )
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
                print(f"Got {len(wav_bytes)} bytes back in {dt:.2f}s")

                with open(args.save, "wb") as f:
                    f.write(wav_bytes)
                print(f"Saved to {args.save}")

                if not args.no_play:
                    play_wav_bytes(wav_bytes, text=text, captions=not args.no_captions)

            if not args.loop:
                break

            # --text was only meant for a single one-off run; once we're
            # looping, always prompt fresh each time
            args.text = None
            print()

    except KeyboardInterrupt:
        print("\nBye.")


if __name__ == "__main__":
    main()