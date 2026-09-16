#!/usr/bin/env python3
"""
Bake a cloned voice into the model's default voice.

Normally, cloning a voice means loading a reference wav and running it
through the VoiceEncoder + S3Gen's reference encoder on every request that
wants that voice (see `prepare_conditionals` in chatterbox/tts.py and
chatterbox/tts_turbo.py). That's real compute and real (transient) VRAM,
paid over and over for a voice that never changes.

This script pays that cost exactly ONCE, offline, and saves the resulting
`Conditionals` object to disk with `Conditionals.save()`. Point `--out` at
the checkpoint's own `conds.pt` and the cloned voice *becomes* the model's
default voice: `ChatterboxTTS.from_local()` / `ChatterboxTurboTTS.from_local()`
load it automatically, and `generate()` with no `speaker_id` or
`audio_prompt_path` uses it as-is. Loading it back is just
`torch.load()` + tensor deserialization (`Conditionals.load()`) -- no
VoiceEncoder forward pass, no S3Gen reference encoding, no extra VRAM spent
re-cloning at inference time. The default voice sounds like the clone;
nothing about the runtime request path changes.

Usage:
    # Replace the shipped default voice outright:
    python precompute_voice.py \\
        --model-dir /path/to/ckpt \\
        --ref-wav samples/Phoenix.WAV \\
        --out /path/to/ckpt/conds.pt \\
        --backup

    # Or keep it as a separate named voice instead of overwriting the default
    # (main.py's _resolve_precomputed_conds already knows how to load these
    # by speaker_id from TTSConfig.voices_dir):
    python precompute_voice.py \\
        --model-dir /path/to/ckpt \\
        --ref-wav samples/Phoenix.WAV \\
        --out /path/to/voices/phoenix.pt
"""
import argparse
import shutil
from pathlib import Path

import numpy as np
import torch


def _patch_librosa_resample_dtype():
    """Newer librosa/numpy combos can return float64 from librosa.resample,
    but the model's mel filterbank (and everything downstream of it) is
    float32 -- that mismatch is exactly what raises "expected scalar type
    Float but found Double" inside s3tokenizer's log_mel_spectrogram. The
    app's main.py already works around this for the live-serving path; a
    standalone script needs the same patch, since it imports librosa
    independently."""
    import librosa

    original_resample = librosa.resample

    def _resample_float32(*args, **kwargs):
        return np.asarray(original_resample(*args, **kwargs), dtype=np.float32)

    librosa.resample = _resample_float32


def main():
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--model-dir", required=True,
        help="ChatterBox checkpoint dir (ve.safetensors, t3_*.safetensors, "
             "s3gen*.safetensors, tokenizer files).",
    )
    parser.add_argument(
        "--ref-wav", required=True,
        help="Reference clip of the voice to bake in. Turbo/Nano require "
             ">5s of clean, single-speaker audio.",
    )
    parser.add_argument(
        "--out", required=True,
        help="Where to write the baked conditionals. Point this at "
             "<model-dir>/conds.pt to replace the default voice outright, "
             "or elsewhere to save it as a separate precomputed voice.",
    )
    parser.add_argument(
        "--variant", choices=["base", "turbo", "nano"], default="turbo",
        help="'base' = chatterbox.tts.ChatterboxTTS, "
             "'turbo'/'nano' = chatterbox.tts_turbo.ChatterboxTurboTTS.",
    )
    parser.add_argument(
        "--device", default="cuda" if torch.cuda.is_available() else "cpu",
    )
    parser.add_argument(
        "--exaggeration", type=float, default=0.5,
        help="Only used by the base variant (turbo/nano ignore emotion_adv).",
    )
    parser.add_argument(
        "--backup", action="store_true",
        help="If --out already exists, copy it to <out>.bak first.",
    )
    args = parser.parse_args()

    _patch_librosa_resample_dtype()

    out_path = Path(args.out)
    if args.backup and out_path.exists():
        backup_path = out_path.with_suffix(out_path.suffix + ".bak")
        shutil.copyfile(out_path, backup_path)
        print(f"Backed up existing conditionals to {backup_path}")

    print(f"Loading {args.variant} model from {args.model_dir} on {args.device} ...")
    if args.variant == "base":
        from voice.tts.chatterbox.tts import ChatterboxTTS
        model = ChatterboxTTS.from_local(args.model_dir, args.device)
        print(f"Cloning reference voice from {args.ref_wav} ...")
        model.prepare_conditionals(args.ref_wav, exaggeration=args.exaggeration)
    else:
        from voice.tts.chatterbox.tts_turbo import ChatterboxTurboTTS
        model = ChatterboxTurboTTS.from_local(
            args.model_dir, args.device, nano=(args.variant == "nano")
        )
        print(f"Cloning reference voice from {args.ref_wav} ...")
        model.prepare_conditionals(args.ref_wav)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    model.conds.to("cpu").save(out_path)

    print(f"\nSaved baked-in conditionals to {out_path}")
    if out_path.name == "conds.pt" and out_path.parent == Path(args.model_dir):
        print(
            "This replaces the checkpoint's default voice. Every call to "
            "generate() without a speaker_id / audio_prompt_path will now "
            "produce the cloned voice, loaded via plain tensor "
            "deserialization -- no VoiceEncoder pass or S3Gen reference "
            "encoding at request time."
        )
    else:
        print(
            "This is a standalone precomputed voice, not the checkpoint "
            "default. Load it via Conditionals.load() (main.py's "
            "_resolve_precomputed_conds already does this for files under "
            "TTSConfig.voices_dir named <speaker_id>.pt), or re-run this "
            "script with --out pointed at <model-dir>/conds.pt to make it "
            "the default instead."
        )


if __name__ == "__main__":
    main()