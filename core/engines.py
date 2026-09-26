"""
Local media engines: where they live and how code runs them.

- tools/runtime   Microsoft VC++ runtime DLLs (app-local, signed) for onnxruntime (Piper)
                  and sd.cpp, so no system-wide install is needed
- tools/sdcpp     stable-diffusion.cpp (sd-cli.exe) for local image generation
- models/         image and voice models
- ffmpeg          bundled with the imageio-ffmpeg package

run_fixed() is the ONLY way code starts these programs: a fixed executable with an
argument list built by code (never a shell, never a model-written command). Model
text reaches them only through files (e.g. sd-cli --prompt-file).
"""

import os
import shutil
import subprocess
import sys
from pathlib import Path

from core.design import ROOT, fonts_installed, FONT_FILES

TOOLS_DIR = ROOT / "tools"
MODELS_DIR = ROOT / "models"
RUNTIME_DIR = TOOLS_DIR / "runtime"
_dll_dirs = []


def resolve(path: str | Path) -> Path:
    p = Path(path)
    return p if p.is_absolute() else ROOT / p


def enable_runtime_dlls() -> None:
    """Let Python extensions (onnxruntime) find the app-local VC++ runtime."""
    if os.name == "nt" and RUNTIME_DIR.is_dir() and not _dll_dirs:
        _dll_dirs.append(os.add_dll_directory(str(RUNTIME_DIR)))


def subprocess_env() -> dict:
    env = os.environ.copy()
    if RUNTIME_DIR.is_dir():
        env["PATH"] = f"{RUNTIME_DIR}{os.pathsep}{env.get('PATH', '')}"
    return env


def run_fixed(args: list, timeout: float, cwd: Path | None = None,
              stdin=None) -> subprocess.CompletedProcess:
    flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
    return subprocess.run([str(a) for a in args], capture_output=True, timeout=timeout,
                          env=subprocess_env(), cwd=cwd, stdin=stdin, creationflags=flags)


def ffmpeg_exe() -> str | None:
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return shutil.which("ffmpeg")


def status() -> dict:
    """What is installed (for doctor.py and the dashboard). Never raises."""
    out = {}
    out["fonts"] = {"installed": len(fonts_installed()), "expected": len(FONT_FILES)}
    out["ffmpeg"] = {"path": ffmpeg_exe()}
    out["runtime_dlls"] = {"dir": str(RUNTIME_DIR),
                           "present": sorted(p.name for p in RUNTIME_DIR.glob("*.dll"))
                           if RUNTIME_DIR.is_dir() else []}
    try:
        enable_runtime_dlls()
        import onnxruntime  # noqa: F401
        import piper  # noqa: F401
        out["piper"] = {"ok": True}
    except Exception as e:
        out["piper"] = {"ok": False, "error": f"{type(e).__name__}: {e}"[:200]}
    for mod in ("fpdf", "pypdfium2", "PIL"):
        try:
            __import__(mod)
            out[mod] = {"ok": True}
        except Exception as e:
            out[mod] = {"ok": False, "error": str(e)[:200]}
    out["voices"] = sorted(p.name for p in (MODELS_DIR / "voices").glob("*.onnx")) \
        if (MODELS_DIR / "voices").is_dir() else []
    out["image_models"] = sorted(p.name for p in (MODELS_DIR / "image").glob("*.*")) \
        if (MODELS_DIR / "image").is_dir() else []
    out["sdcpp"] = {"path": str(TOOLS_DIR / "sdcpp" / "sd-cli.exe"),
                    "present": (TOOLS_DIR / "sdcpp" / "sd-cli.exe").is_file()}
    return out
