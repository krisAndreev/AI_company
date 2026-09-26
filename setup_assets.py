"""
Download the local media engines and models the studio uses. Every file is pinned
to an exact URL and SHA-256 hash: a changed or tampered download is refused.

    python setup_assets.py                 # everything (about 2.8 GB)
    python setup_assets.py --only fonts,voice,runtime
    python setup_assets.py --list

What gets installed (all inside this folder, nothing system-wide):
  fonts     SIL Open Font Licence fonts for products and graphics   assets/fonts     ~3 MB
  runtime   Microsoft VC++ runtime DLLs (signed, app-local) for
            Piper TTS and stable-diffusion.cpp                       tools/runtime    ~3 MB
  sdcpp     stable-diffusion.cpp (Vulkan build) - local image gen    tools/sdcpp     ~32 MB
  voice     Piper voice en_US-lessac-medium                          models/voices   ~63 MB
  image     SDXL-Lightning 4-step (q4_0 GGUF) photo model            models/image   ~2.6 GB
"""

import argparse
import hashlib
import shutil
import sys
import tempfile
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
GF = "https://raw.githubusercontent.com/google/fonts/main/ofl/"

FONTS = {
    "Poppins-Regular.ttf": ("poppins", "7e65201e9b79159e2300267cc885e16c8dcef2424cdfa09a29bfb0980a94a7ba"),
    "Poppins-SemiBold.ttf": ("poppins", "d3bf1bdaf0550e83da9ac0b1d1d9fe6db086835a83aa28578e609a394b9a0286"),
    "Poppins-Bold.ttf": ("poppins", "983676516167748b74de6f4771fb384c664fd913acb8b471122ecacf5da5ea6c"),
    "Lato-Regular.ttf": ("lato", "d636e4683231f931eda222d588e944d082bfd3bdba02f928bee461c0f185b251"),
    "Lato-Bold.ttf": ("lato", "8a0aace75d33794eece4b28187bfc1df0bbd2888b5d8a56e01788c8d65d16be1"),
    "Lato-Italic.ttf": ("lato", "e399c44efe1387100531d26c7e4800c5d12251b890d6654a3098c7c679cb1786"),
    "DMSerifDisplay-Regular.ttf": ("dmserifdisplay", "8cc3643535edf039aa5d95440a8542735e9197e4f4b8d9303e980fefbf5ab616"),
    "AmaticSC-Bold.ttf": ("amaticsc", "d367beadee66efbb657d24d3d8f30f3b60633e9d5c8ded7385541cf775cabb4d"),
    "Pacifico-Regular.ttf": ("pacifico", "5b6c0d5334a7bf77dea52b975c5a0c408878c0f7115ed5b6fb151f634b7bf701"),
}

DOWNLOADS = {
    "runtime": ("https://files.pythonhosted.org/packages/21/3b/134d04268ab8e35853cd007582076429b45d60d6abb1036d159be9c50342/msvc_runtime-14.44.35112-cp312-cp312-win_amd64.whl",
                "32f9c706009e16ccc319d6947ce3bffe20e5192bee52b18cf48313f9e7bedfbe", None),
    "sdcpp": ("https://github.com/leejet/stable-diffusion.cpp/releases/download/master-918-39ada08/sd-master-39ada08-bin-win-vulkan-x64.zip",
              "bf8fec98f4adda754838b301ad6f99dd325b0a423dcb015075df4fcb83311e27", None),
    "voice": ("https://huggingface.co/rhasspy/piper-voices/resolve/main/en/en_US/lessac/medium/en_US-lessac-medium.onnx",
              "5efe09e69902187827af646e1a6e9d269dee769f9877d17b16b1b46eeaaf019f",
              "models/voices/en_US-lessac-medium.onnx"),
    "voice_config": ("https://huggingface.co/rhasspy/piper-voices/resolve/main/en/en_US/lessac/medium/en_US-lessac-medium.onnx.json",
                     "efe19c417bed055f2d69908248c6ba650fa135bc868b0e6abb3da181dab690a0",
                     "models/voices/en_US-lessac-medium.onnx.json"),
    "image": ("https://huggingface.co/mzwing/SDXL-Lightning-GGUF/resolve/main/sdxl_lightning_4step.q4_0.gguf",
              "33cc25b6f5dd121ecf3ad28e35eb38cb21427b7794c53527ceafe2dc3b109f4f",
              "models/image/sdxl_lightning_4step.q4_0.gguf"),
}
GROUPS = ["fonts", "runtime", "sdcpp", "voice", "image"]


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def fetch(url: str, sha: str, dest: Path) -> Path:
    """Download to dest (atomically) unless it already exists with the right hash."""
    if dest.is_file() and sha256_file(dest) == sha:
        print(f"  ok       {dest.relative_to(ROOT)}")
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".part")
    print(f"  download {url.rsplit('/', 1)[-1]} ...", flush=True)
    h, n = hashlib.sha256(), 0
    with urllib.request.urlopen(url, timeout=120) as r, open(tmp, "wb") as f:
        while chunk := r.read(1 << 20):
            f.write(chunk)
            h.update(chunk)
            n += len(chunk)
    if h.hexdigest() != sha:
        tmp.unlink(missing_ok=True)
        raise SystemExit(f"  HASH MISMATCH for {url} - refusing the file")
    tmp.replace(dest)
    print(f"  saved    {dest.relative_to(ROOT)} ({n / 1e6:.1f} MB, sha256 verified)")
    return dest


def install(group: str) -> None:
    print(f"[{group}]")
    if group == "fonts":
        folder = ROOT / "assets" / "fonts"
        for name, (family, sha) in FONTS.items():
            fetch(f"{GF}{family}/{name}", sha, folder / name)
        lic = folder / "LICENSES-OFL.txt"
        if not lic.is_file():
            texts = []
            for family in sorted({f for f, _ in FONTS.values()}):
                with urllib.request.urlopen(f"{GF}{family}/OFL.txt", timeout=60) as r:
                    texts.append(f"===== {family} =====\n{r.read().decode('utf-8')}\n")
            lic.write_text("These fonts are licensed under the SIL Open Font License 1.1.\n\n"
                           + "\n".join(texts), encoding="utf-8")
    elif group == "runtime":
        url, sha, _ = DOWNLOADS["runtime"]
        with tempfile.TemporaryDirectory() as tmp:
            whl = fetch(url, sha, Path(tmp) / "msvc_runtime.whl")
            out = ROOT / "tools" / "runtime"
            out.mkdir(parents=True, exist_ok=True)
            with zipfile.ZipFile(whl) as z:
                for info in z.infolist():
                    if info.filename.endswith(".dll") and "/Scripts/" in info.filename:
                        (out / Path(info.filename).name).write_bytes(z.read(info))
            print(f"  extracted {len(list(out.glob('*.dll')))} DLLs to tools/runtime")
    elif group == "sdcpp":
        url, sha, _ = DOWNLOADS["sdcpp"]
        folder = ROOT / "tools" / "sdcpp"
        zpath = fetch(url, sha, folder / "sd-vulkan.zip")
        with zipfile.ZipFile(zpath) as z:
            for info in z.infolist():
                target = (folder / info.filename).resolve()
                if folder.resolve() not in target.parents:
                    raise SystemExit("zip entry escapes the folder - refusing")
            z.extractall(folder)
        print("  extracted sd-cli.exe")
    elif group == "voice":
        for key in ("voice", "voice_config"):
            url, sha, rel = DOWNLOADS[key]
            fetch(url, sha, ROOT / rel)
    elif group == "image":
        url, sha, rel = DOWNLOADS["image"]
        free = shutil.disk_usage(ROOT).free / 1e9
        if free < 4 and not (ROOT / rel).is_file():
            raise SystemExit(f"  only {free:.1f} GB free; the image model needs 2.6 GB")
        fetch(url, sha, ROOT / rel)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--only", help="comma-separated groups: " + ",".join(GROUPS))
    ap.add_argument("--list", action="store_true")
    args = ap.parse_args()
    if args.list:
        print(__doc__)
        return 0
    groups = args.only.split(",") if args.only else GROUPS
    for g in groups:
        if g not in GROUPS:
            print(f"unknown group {g!r}; choose from {GROUPS}")
            return 1
        install(g)
    print("done. Run  python doctor.py  to check the whole installation.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
