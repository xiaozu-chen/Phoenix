# -*- mode: python ; coding: utf-8 -*-
import os
from PyInstaller.utils.hooks import collect_submodules, collect_data_files, copy_metadata

# Matches TTSConfig.variant in settings/config.py -- set CHATTERBOX_VARIANT
# before running pyinstaller if you're bundling the nano checkpoint instead
# of turbo (they live in separate folders since they're separate HF repos).
_chatterbox_variant = os.environ.get("CHATTERBOX_VARIANT", "turbo").strip().lower()

datas = [
    (".env", "."),
    ("app.ico", "."),
    # Built React app -- copy `npm run build`'s dist/ contents into
    # backend/static/ BEFORE running PyInstaller, or this path won't
    # exist and the spec will fail here. main.py mounts this at "/"
    # via StaticFiles, looking it up as sys._MEIPASS / "static" when
    # frozen.
    ("static", "static"),
    ("bin/llama", "bin/llama"),
    # ChatterBox is vendored under voice/tts/chatterbox (see voice/tts/main.py
    # for why it's loaded as a fake top-level "chatterbox" module rather than
    # a normal `voice.tts.chatterbox` import). PyInstaller's static analysis
    # can't see that dynamic import, so the source has to be bundled as raw
    # data files here, same as the old MeloTTS setup did.
    ("voice/tts/chatterbox", "chatterbox"),
    ("voice/stt/faster_whisper/assets", "voice/stt/faster_whisper/assets"),
    # TTSConfig.model_dir (settings/config.py) resolves, when frozen, to
    # sys._MEIPASS / "models" / f"chatterbox-{variant}" -- that checkpoint
    # folder has to actually be sitting at models/chatterbox-<variant>
    # relative to this spec before running PyInstaller (download it there
    # first, same as bin/llama above), or ChatterboxTurboTTS.from_local()
    # will fail at runtime looking for weights that were never bundled.
    (f"models/chatterbox-{_chatterbox_variant}", f"models/chatterbox-{_chatterbox_variant}"),
]

hiddenimports = [
    "uvicorn.logging",
    "uvicorn.loops",
    "uvicorn.loops.auto",
    "uvicorn.protocols",
    "uvicorn.protocols.http",
    "uvicorn.protocols.http.auto",
    "uvicorn.protocols.websockets",
    "uvicorn.protocols.websockets.auto",
    "uvicorn.lifespan",
    "uvicorn.lifespan.on",
    "multipart",
    "python_multipart",
    "orjson",
    "loguru",
    # ui/tray.py -- pystray resolves its backend module (win32/appindicator/
    # gtk/xorg/darwin) dynamically based on platform rather than a plain
    # `import`, so PyInstaller's static analysis won't find it on its own.
    "pystray",
    "pystray._win32",
    "PIL",
    "PIL.Image",
]

# transformers (and tokenizers) resolve names like AutoTokenizer through a
# custom lazy-import __getattr__ rather than plain `import` statements, so
# PyInstaller's static analysis can't discover which submodules are needed.
# Force-collecting everything avoids ImportErrors for names that only
# resolve at runtime. tokenizers ships a compiled Rust extension that
# transformers' backend-availability check depends on -- if it's missing,
# transformers silently skips registering AutoTokenizer instead of raising
# a clear "tokenizers not found" error, which is what produced the
# confusing "cannot import name 'AutoTokenizer'" error.
hiddenimports += collect_submodules("transformers")
hiddenimports += collect_submodules("tokenizers")
hiddenimports += collect_submodules("huggingface_hub")
# Ensure safetensors is fully bundled for reliable model weight loading
hiddenimports += ["safetensors", "safetensors.torch"]

datas += collect_data_files("transformers")
datas += collect_data_files("tokenizers")

# g2p_en ships a data file (checkpoint20.npz) inside its own package
# folder that it loads relative to its own location at runtime.
# collect_submodules only grabs .py files, not arbitrary data files,
# so this needs its own explicit collection.
datas += collect_data_files("g2p_en")

# perth (resemble-perth, chatterbox's watermarker) ships its pretrained
# checkpoint as package data -- perth/perth_net/pretrained/implicit/
# (hparams.yaml, id.txt, perth_net_250000.pth.tar) -- not as .py files,
# so PyInstaller's import-based static analysis never picks it up on its
# own. Without it, CheckpointManager finds no hparams.yaml on disk *and*
# gets no dataset_hp passed in, so its "assert dataset_hp is not None"
# fires as a bare AssertionError with no message.
datas += collect_data_files("perth")

# transformers' and diffusers' dependency_versions_check.py runs
# importlib.metadata.version(pkg) on a hardcoded list of their own deps
# the moment either package is imported (chatterbox pulls in diffusers,
# which pulls this in transitively). PyInstaller doesn't bundle a
# package's .dist-info/METADATA by default -- only its code -- so
# without these, that check raises PackageNotFoundError for whichever
# dep it hits first (seen in practice: "requests"), even though the
# package itself imports fine. copy_metadata() grabs just the dist-info,
# not the code (already covered above/by normal Analysis).
for _pkg in (
    "requests",
    "filelock",
    "numpy",
    "tqdm",
    "regex",
    "packaging",
    "tokenizers",
    "huggingface-hub",
    "safetensors",
    "accelerate",
    "PyYAML",
    "transformers",
    "diffusers",
):
    datas += copy_metadata(_pkg)

a = Analysis(
    ["phoenix.py"],
    pathex=["voice/tts"],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=["runtime_hook_typeguard.py"],
    excludes=[
        "gradio",
        "gradio_client",
        "torchvision",  # ADDED: Exclude torchvision to prevent PyInstaller C++ extension crash (nms operator) in audio-only TTS apps
    ],
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure)

# onedir build: EXE only holds the launcher + pure-python code (pyz).
# exclude_binaries=True keeps a.binaries/a.datas OUT of the exe itself;
# COLLECT below places them as loose files next to it in dist/backend/.
# This avoids the onefile behavior of re-extracting ~2GB to a temp dir
# on every single launch.
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="PhoenixSvc",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=["app.ico"],
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name="PhoenixSvc",
)
