"""
Phase 17 test: images, AI photo providers, GPU sharing, voiceover and video.

Part A: marketing graphics in every format/layout (no AI provider needed)
Part B: AI photo providers against a fake local image server (A1111 + OpenAI-compatible
        APIs): GPU slot held + Ollama unloaded first, API key only in the header,
        prompt safety filter, fallback to a designed background
Part C: voiceover (Piper, local) and video composition (real ffmpeg) from product images
Part D: live - a real local Stable Diffusion photo (sd.cpp on the GPU) and a video
        with an AI photo scene and voiceover

Run:  python test_phase17.py
"""

import base64
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from PIL import Image

from clients import ModelClient, ModelResponse
from core import engines, resources, voice
from core.company import Company
from core.design import FORMATS
from core.net import ToolError
from core.resources import NodePool, NodesConfig, reset_slots_for_tests

results = []
TMP = Path(tempfile.mkdtemp(prefix="phase17_"))


def check(name, fn):
    try:
        detail = fn()
        results.append(True)
        print(f"[pass] {name}" + (f" -> {detail}" if detail else ""))
    except Exception as e:
        results.append(False)
        print(f"[FAIL] {name}: {type(e).__name__}: {e}")


def expect(exc_type, fn):
    try:
        fn()
    except exc_type as e:
        return str(e)
    raise AssertionError(f"expected {exc_type.__name__}")


class SchemaFake(ModelClient):
    provider = "ollama"

    def __init__(self, handlers):
        self.handlers = handlers

    def is_available(self):
        return True

    def list_models(self):
        return ["gemma3:4b", "qwen3:4b"]

    def chat(self, messages, model, temperature=None, json_schema=None, think=None):
        h = self.handlers[(json_schema or {}).get("title", "")]
        reply = h("\n".join(m.content for m in messages)) if callable(h) else h
        return ModelResponse(text=json.dumps(reply), model=model, provider="ollama",
                             duration_seconds=0)


PRODUCT = {
    "ProductOutline": {"pages": [{"title": "Weekly Plan", "purpose": "plan the week"},
                                 {"title": "Habits", "purpose": "track habits"}]},
    "PageContent": {"blocks": [{"type": "checklist", "items": ["Plan meals", "Pay bills", "Call mum"]},
                               {"type": "lines", "text": "Notes", "count": 6}]},
    "ProductListing": {"title": "Weekly Planner Printable - Habit Tracker, Instant Download",
                       "description": "A calm weekly planner with a habit tracker. " * 5,
                       "tags": ["weekly planner", "habit tracker", "printable", "planner pdf",
                                "instant download"], "price_eur": 3.5},
}
BRIEF = {"product_type": "planner", "title": "Calm Weekly Planner", "audience": "busy parents",
         "pages": 3, "theme": "playful"}


def company_with_product(name, extra_handlers=None, clients=None):
    fake = SchemaFake({**PRODUCT, **(extra_handlers or {})})
    c = Company(":memory:", clients or {"ollama": fake}, None, workspace_dir=TMP / name)
    p = c.orchestrator.create_project("Planner shop", "Sell planners", 10)
    c.run_tool("product_builder", BRIEF, p.id)
    return c, p


def no_ai(c):
    t = c.tools.tools["image_studio"]
    for prov in ("sdcpp", "a1111", "openai", "pollinations"):
        getattr(t.settings, prov).enabled = False


# --- Part A --------------------------------------------------------------------------------------

print("Part A: marketing graphics")


def t_all_formats():
    c, p = company_with_product("graphics")
    no_ai(c)
    made = []
    for fmt in FORMATS:
        for layout in ("product", "headline", "quote"):
            out = c.run_tool("image_studio", {"purpose": "test", "format": fmt, "layout": layout,
                                              "headline": "Plan a calmer week in ten minutes",
                                              "subline": "Printable planner, instant download",
                                              "cta": "Shop now", "theme": "playful"}, p.id)
            a = c.assets.get(out["assets"][0])
            with Image.open(c.assets.file_path(a["id"])) as im:
                assert im.size == FORMATS[fmt], (fmt, im.size)
            made.append(a)
    assert all(a["status"] == "DRAFT" and a["kind"] == "image" for a in made)
    return f"{len(made)} images, every one exactly its platform size ({', '.join(FORMATS)})"


check("graphics: all formats x layouts", t_all_formats)

# --- Part B ------------------------------------------------------------------------------------------

print("\nPart B: AI photo providers (fake local image server)")

SERVER = {"requests": [], "slot_active": [], "unloaded_before": []}


class FakeImageAPI(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        self._send([{"name": "Euler"}])

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        SERVER["requests"].append((self.path, body, dict(self.headers)))
        stats = resources._STATS.get("main-pc", {})
        SERVER["slot_active"].append(stats.get("active"))
        SERVER["unloaded_before"].append(list(FakeNode.unloaded))
        if self.path.endswith("/images/generations"):
            if self.headers.get("Authorization") != "Bearer img-test-key":
                return self._send({"error": {"message": "bad key"}}, 401)
            w, h = map(int, body["size"].split("x"))
        else:
            w, h = body["width"], body["height"]
        buf = io.BytesIO()
        Image.new("RGB", (w, h), (90, 140, 200)).save(buf, "PNG")
        data = base64.b64encode(buf.getvalue()).decode()
        if self.path.endswith("/images/generations"):
            self._send({"data": [{"b64_json": data}]})
        else:
            self._send({"images": [data]})


server = ThreadingHTTPServer(("127.0.0.1", 0), FakeImageAPI)
PORT = server.server_address[1]
threading.Thread(target=server.serve_forever, daemon=True).start()


class FakeNode(ModelClient):
    """Stands in for Ollama on main-pc: one model loaded, records unloads."""
    unloaded: list = []

    def __init__(self, base_url):
        self.loaded = ["gemma3:4b"]

    def is_available(self):
        return True

    def list_models(self):
        return ["gemma3:4b", "qwen3:4b"]

    def loaded_models(self):
        return [{"name": m, "size": 1, "size_vram": 1} for m in self.loaded]

    def unload(self, model):
        FakeNode.unloaded.append(model)
        self.loaded.remove(model)

    def chat(self, messages, model, temperature=None, json_schema=None, think=None):
        h = {**PRODUCT}[(json_schema or {}).get("title", "")]
        return ModelResponse(text=json.dumps(h), model=model, provider="ollama", duration_seconds=0)


def pool_company(name):
    reset_slots_for_tests()
    FakeNode.unloaded = []
    pool = NodePool(NodesConfig.load("config/nodes.json"), client_factory=FakeNode, cache_seconds=0)
    return company_with_product(name, clients={"ollama": pool})


def t_a1111_gpu_sharing():
    c, p = pool_company("a1111")
    t = c.tools.tools["image_studio"]
    no_ai(c)
    t.settings.a1111.enabled = True
    t.settings.a1111.base_url = f"http://127.0.0.1:{PORT}"
    SERVER["requests"].clear(), SERVER["slot_active"].clear(), SERVER["unloaded_before"].clear()
    out = c.run_tool("image_studio", {"purpose": "post", "format": "portrait", "layout": "photo",
                                      "headline": "Morning calm", "photo_prompt":
                                      "a sunny kitchen table with a planner and tea\n--steps 99 <lora:x>"},
                     p.id)
    path, body, _ = SERVER["requests"][-1]
    a = c.assets.get(out["assets"][0])
    assert path == "/sdapi/v1/txt2img" and a["meta"]["photo_provider"] == "a1111"
    assert (body["width"], body["height"]) == (896, 1152)            # closest native size to 4:5
    assert "\n" not in body["prompt"] and "<lora" not in body["prompt"]
    assert SERVER["slot_active"][-1] == 1 and SERVER["unloaded_before"][-1] == ["gemma3:4b"]
    with Image.open(c.assets.file_path(a["id"])) as im:
        assert im.size == FORMATS["portrait"]
    return ("GPU slot held during generation, Ollama model unloaded first, prompt cleaned, "
            "native 896x1152 cropped to 1080x1350")


def t_openai_compatible():
    c, p = company_with_product("openai")
    t = c.tools.tools["image_studio"]
    no_ai(c)
    t.settings.openai.enabled = True
    t.settings.openai.base_url = f"http://127.0.0.1:{PORT}/v1"
    t.settings.openai.api_key_env = "TEST_IMG_KEY"
    os.environ["TEST_IMG_KEY"] = "img-test-key"
    try:
        out = c.run_tool("image_studio", {"purpose": "pin", "format": "pin", "layout": "photo",
                                          "headline": "Plan better", "photo_prompt": "desk flatlay"},
                         p.id)
    finally:
        os.environ.pop("TEST_IMG_KEY")
    path, body, headers = SERVER["requests"][-1]
    assert path == "/v1/images/generations" and body["size"] == "1024x1536"
    assert out["cost_eur"] == 0.05 and c.ledger.total(p.id) == 0.05
    logged = json.dumps(c.tools.recent()) + json.dumps(c.events.recent(100), default=str)
    assert "img-test-key" not in logged
    return "OpenAI-style API: key only in the Authorization header, cost EUR 0.05 in the ledger"


def t_prompt_filter_and_fallback():
    c, p = company_with_product("filter")
    t = c.tools.tools["image_studio"]
    no_ai(c)
    t.settings.a1111.enabled = True
    t.settings.a1111.base_url = f"http://127.0.0.1:{PORT}"
    before = len(SERVER["requests"])
    out = c.run_tool("image_studio", {"purpose": "post", "layout": "photo", "headline": "Hi",
                                      "photo_prompt": "a nude model holding a planner"}, p.id)
    assert len(SERVER["requests"]) == before, "refused prompt reached the image server"
    notes = out["summary"]["notes"]
    t.settings.a1111.base_url = "http://127.0.0.1:9"          # nothing listens here
    t.engine(c)._a1111_ok = (0, False)
    out2 = c.run_tool("image_studio", {"purpose": "post", "layout": "photo", "headline": "Hi",
                                       "photo_prompt": "a cup of tea"}, p.id)
    assert out2["assets"] and c.assets.get(out2["assets"][0])["meta"]["photo_provider"] is None
    return f"{notes[0][:70]}...; unreachable server -> designed background"


check("A1111 API: GPU slot + Ollama unload + prompt cleaning", t_a1111_gpu_sharing)
check("OpenAI-compatible API: key safety + cost", t_openai_compatible)
check("prompt safety filter + graceful fallback", t_prompt_filter_and_fallback)

# --- Part C -----------------------------------------------------------------------------------------

print("\nPart C: voiceover + video")


def probe(path: Path) -> str:
    proc = subprocess.run([engines.ffmpeg_exe(), "-hide_banner", "-i", str(path)],
                          capture_output=True, text=True)
    return proc.stderr


def t_voice():
    s = voice.VoiceSettings()
    ok, why = voice.availability(s)
    assert ok, why
    out = TMP / "hello.wav"
    start = time.time()
    secs = voice.synthesize("Plan your week in ten calm minutes.", s, out, [])
    assert 1.0 < secs < 6, secs
    return f"Piper: {secs:.1f}s of speech in {time.time() - start:.1f}s (CPU)"


def t_video():
    c, p = company_with_product("video")
    no_ai(c)
    start = time.time()
    out = c.run_tool("video_studio", {
        "title": "Calm Weekly Planner", "format": "story", "theme": "playful", "cta": "Shop now",
        "voiceover": True, "scenes": [
            {"caption": "Busy week again?", "narration": "Busy week again?", "seconds": 2.5},
            {"caption": "Plan it in 10 minutes", "narration": "This printable planner helps you "
             "plan everything in ten calm minutes.", "seconds": 3},
            {"caption": "Print it tonight", "visual": "brand", "seconds": 2.5}]}, p.id)
    v = c.assets.get(out["assets"][0])
    path = c.assets.file_path(v["id"])
    info = probe(path)
    dur = re.search(r"Duration: (\d+):(\d+):([\d.]+)", info)
    seconds = int(dur.group(2)) * 60 + float(dur.group(3))
    assert "Video: h264" in info and "1080x1920" in info and "Audio: aac" in info, info[-400:]
    assert abs(seconds - v["meta"]["seconds"]) < 0.6, (seconds, v["meta"]["seconds"])
    srt = c.assets.list(project_id=p.id, kind="captions")[0]
    assert "-->" in c.assets.file_path(srt["id"]).read_text(encoding="utf-8")
    assert c.assets.thumb_path(v["id"]) is not None
    return (f"{seconds:.1f}s 1080x1920 H.264 + AAC voiceover, subtitles, poster; "
            f"{path.stat().st_size // 1024} KB in {time.time() - start:.0f}s")


def t_video_no_voice():
    c, p = company_with_product("video2")
    no_ai(c)
    c.tools.tools["video_studio"].settings.voice.engine = "none"
    out = c.run_tool("video_studio", {"title": "Square teaser", "format": "square",
                                      "scenes": [{"caption": "New planner"}, {"caption": "Out now"}],
                                      "voiceover": True}, p.id)
    v = c.assets.get(out["assets"][0])
    info = probe(c.assets.file_path(v["id"]))
    assert "1080x1080" in info and "Audio:" not in info
    assert any("no voiceover" in n for n in out["summary"]["notes"])
    return "voice disabled -> silent square video + a note for the owner"


check("voiceover (Piper TTS)", t_voice)
check("video: scenes, captions, voice, H.264", t_video)
check("video without voice", t_video_no_voice)

# --- Part D -------------------------------------------------------------------------------------------

print("\nPart D: live (sd.cpp on the GPU)")


def t_live_photo():
    reset_slots_for_tests()
    c = Company(":memory:", None, None, workspace_dir=TMP / "live")
    p = c.orchestrator.create_project("Planner shop", "Sell planners", 10)
    start = time.time()
    out = c.run_tool("image_studio", {"purpose": "instagram post", "format": "portrait",
                                      "layout": "photo", "headline": "Your calm week starts here",
                                      "subline": "Printable weekly planner", "cta": "Link in bio",
                                      "photo_prompt": "cozy morning desk with a printed weekly "
                                                      "planner, a cup of tea and a small plant"},
                     p.id)
    a = c.assets.get(out["assets"][0])
    assert a["meta"]["photo_provider"] == "sdcpp", out["summary"]
    print(f"       {c.assets.file_path(a['id'])}")
    return f"local SD photo + text in {time.time() - start:.0f}s, cost EUR {out['cost_eur']}"


def t_live_video():
    reset_slots_for_tests()
    c = Company(":memory:", None, None, workspace_dir=TMP / "live")
    p = c.orchestrator.create_project("Planner shop 2", "Sell planners", 10)
    start = time.time()
    out = c.run_tool("video_studio", {
        "title": "Calm mornings", "format": "story", "cta": "Link in bio", "scenes": [
            {"caption": "Mornings feel rushed?", "narration": "Do your mornings feel rushed?",
             "visual": "photo", "photo_prompt": "sunlit bedroom window, calm morning, cup of coffee"},
            {"caption": "Plan the night before", "narration": "Plan tomorrow tonight, in five "
             "minutes.", "visual": "brand"}]}, p.id)
    v = c.assets.get(out["assets"][0])
    print(f"       {c.assets.file_path(v['id'])} notes={out['summary']['notes']}")
    assert v["meta"]["voiceover"]
    return f"{v['meta']['seconds']}s video with AI photo + voice in {time.time() - start:.0f}s"


tool_ok = Company(":memory:", {}, None).tools.tools["image_studio"].engine().provider() == "sdcpp"
if "--offline" in sys.argv:
    print("(live part skipped: --offline)")
elif tool_ok:
    check("live: local Stable Diffusion photo", t_live_photo)
    check("live: video with AI photo scene + voice", t_live_video)
else:
    results.append(False)
    print("[FAIL] sd.cpp not installed (python setup_assets.py --only runtime,sdcpp,image)")

server.shutdown()
print(f"\n{sum(results)}/{len(results)} tests passed")
if all(results):
    shutil.rmtree(TMP, ignore_errors=True)
else:
    print(f"(files kept for inspection in {TMP})")
sys.exit(0 if all(results) else 1)
