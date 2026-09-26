"""
Production preflight: checks everything the company needs and says what to fix.

    python doctor.py              # all checks (fast)
    python doctor.py --gpu-test   # also generate one small test image on the GPU
    python doctor.py --db data/company.db

Exit code 0 = no FAIL (WARNs are allowed), 1 = something must be fixed.
Only names of API-key environment variables are shown, never their values.
"""

import argparse
import os
import shutil
import sqlite3
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
results: list[tuple[str, str, str]] = []


def report(level: str, name: str, detail: str = "") -> None:
    results.append((level, name, detail))
    print(f"[{level:4}] {name}" + (f" - {detail}" if detail else ""), flush=True)


def check(name, fn):
    try:
        out = fn()
        if isinstance(out, tuple):
            report(out[0], name, out[1])
        else:
            report("OK", name, out or "")
    except Exception as e:
        report("FAIL", name, f"{type(e).__name__}: {e}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="data/company.db")
    ap.add_argument("--gpu-test", action="store_true")
    args = ap.parse_args()
    os.chdir(ROOT)

    check("python", lambda: f"{sys.version.split()[0]}" if sys.version_info >= (3, 11)
          else ("FAIL", f"{sys.version.split()[0]} - need 3.11+"))

    def packages():
        missing = []
        for mod in ("pydantic", "fastapi", "uvicorn", "fpdf", "pypdfium2", "PIL", "imageio_ffmpeg",
                    "psutil"):
            try:
                __import__(mod)
            except ImportError:
                missing.append(mod)
        return ("FAIL", f"missing {missing}: pip install -r requirements.txt") if missing else "all present"
    check("python packages", packages)

    def config():
        from core.company import Company
        c = Company(":memory:", {}, None)
        return f"{len(c.departments.names())} departments, {len(c.tools.tools)} tools"
    check("config files", config)

    def ollama():
        from core.model_registry import ModelRegistry
        from core.resources import NodePool, NodesConfig
        pool = NodePool(NodesConfig.load("config/nodes.json"))
        installed = set(pool.list_models())
        need = [m.model for m in ModelRegistry.load("config/models.json").models.values()
                if m.enabled and m.provider == "ollama"]
        missing = [m for m in need if m not in installed]
        return ("FAIL", f"missing models {missing}: ollama pull <name>") if missing else \
            f"models ready: {', '.join(need)}"
    check("ollama + models", ollama)

    from core import engines
    status = engines.status()
    f = status["fonts"]
    check("fonts", lambda: f"{f['installed']}/{f['expected']} open-licence fonts" if f["installed"] == f["expected"]
          else ("WARN", f"{f['installed']}/{f['expected']} installed - run: python setup_assets.py --only fonts "
                        "(products fall back to Arial, not for sale)"))
    check("ffmpeg (video)", lambda: status["ffmpeg"]["path"] or ("FAIL", "pip install imageio-ffmpeg"))

    def tts():
        from core.tools import ToolsConfig
        from core.voice import VoiceSettings, availability
        settings = ToolsConfig.load("config/tools.json").tools["video_studio"].settings.get("voice", {})
        ok, why = availability(VoiceSettings.model_validate(settings))
        return why if ok else ("WARN", f"{why} - run: python setup_assets.py --only runtime,voice "
                                       "(videos will have no voiceover)")
    check("voiceover (piper)", tts)

    def images():
        from core.company import Company
        c = Company(":memory:", {}, None)
        tool = c.tools.tools["image_studio"]
        rows = tool.engine(c).status()
        ready = [r["provider"] for r in rows if r["available"]]
        if ready:
            return f"AI photos via {ready[0]}" + (f" (also: {', '.join(ready[1:])})" if ready[1:] else "")
        why = "; ".join(f"{r['provider']}: {r['reason']}" for r in rows)
        return ("WARN", f"no AI photo provider ({why}) - run: python setup_assets.py --only "
                        "runtime,sdcpp,image  (graphics still work)")
    check("AI photos", images)

    def search():
        from core.company import Company
        c = Company(":memory:", {}, None)
        p = c.tools.tools["web_research"].client().provider()
        if p == "duckduckgo":
            return ("WARN", "using DuckDuckGo (no key, best effort). For production set TAVILY_API_KEY "
                            "(1,000 free searches/month)")
        return p or ("FAIL", "no search provider")
    check("web search", search)

    keys = ["TAVILY_API_KEY", "BRAVE_API_KEY", "OPENAI_API_KEY", "POLLINATIONS_API_KEY",
            "ELEVENLABS_API_KEY", "ETSY_API_KEY"]
    present = [k for k in keys if os.environ.get(k)]
    report("OK" if present else "INFO", "API keys (env)", ", ".join(present) or
           "none set (optional: " + ", ".join(keys) + ")")

    check("dashboard password", lambda: "set" if Path("data/dashboard_auth.json").is_file()
          else ("WARN", "run: python run_dashboard.py --set-password"))

    def database():
        p = Path(args.db)
        if not p.is_file():
            return "WARN", f"{p} does not exist yet (created on first start)"
        con = sqlite3.connect(str(p))
        try:
            r = con.execute("PRAGMA quick_check").fetchone()[0]
        finally:
            con.close()
        return r if r == "ok" else ("FAIL", f"integrity problem: {r}")
    check("database integrity", database)

    def backups():
        folder = Path(args.db).resolve().parent / "backups"
        files = sorted(folder.glob(f"{Path(args.db).stem}-*.db"), key=lambda x: x.stat().st_mtime) \
            if folder.is_dir() else []
        if not files:
            return "WARN", "no backup yet (the running dashboard makes one daily)"
        age = (time.time() - files[-1].stat().st_mtime) / 3600
        return (f"{len(files)} backups, newest {age:.0f} h old" if age < 48
                else ("WARN", f"newest backup is {age:.0f} h old"))
    check("backups", backups)

    def disk():
        free = shutil.disk_usage(ROOT).free / 1e9
        return f"{free:.0f} GB free" if free > 10 else ("WARN", f"only {free:.1f} GB free")
    check("disk space", disk)

    def workspace():
        from core.config import OrchestratorConfig
        ws = Path(OrchestratorConfig.load("config/company.json").workspace_dir)
        ws.mkdir(parents=True, exist_ok=True)
        probe = ws / ".write_test"
        probe.write_text("ok")
        probe.unlink()
        return f"{ws.resolve()} writable"
    check("workspace", workspace)

    def brand():
        from core.config import OrchestratorConfig
        name = OrchestratorConfig.load("config/company.json").brand_name
        return name if name else ("INFO", "brand_name empty in config/company.json (shown on products)")
    check("brand name", brand)

    if args.gpu_test:
        def gpu():
            from core.company import Company
            c = Company(":memory:", None, None)
            engine = c.tools.tools["image_studio"].engine(c)
            start = time.time()
            img, provider, _ = engine.generate("a ceramic coffee mug on a wooden table", (512, 512), 1)
            out = ROOT / "logs" / "doctor-gpu-test.png"
            out.parent.mkdir(exist_ok=True)
            img.save(out)
            return f"{provider}: {img.width}x{img.height} in {time.time() - start:.0f}s -> {out}"
        check("GPU image test", gpu)

    fails = [r for r in results if r[0] == "FAIL"]
    warns = [r for r in results if r[0] == "WARN"]
    print(f"\n{len(results) - len(fails) - len(warns)} ok, {len(warns)} warnings, {len(fails)} failures")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
