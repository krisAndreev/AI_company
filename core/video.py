"""
Short marketing videos (Reels / TikTok / Shorts / feed): scenes of product visuals or
AI photos with slow zoom, burned-in captions, crossfades, a branded end card and an
optional voiceover. Output: H.264 MP4 (+ AAC audio) and an .srt subtitle file.

The model proposes the storyboard (VideoParams: captions, narration, which visual);
code picks the actual images, timing, voice, encoding, and runs ffmpeg with a fixed
argument list (frames are piped in; no model text ever reaches the command line).
"""

import subprocess
import tempfile
from pathlib import Path
from typing import Literal

from PIL import Image
from pydantic import Field

from core import engines, graphics
from core import voice as tts
from core.design import ThemeName, theme
from core.net import ToolError
from core.schemas import StrictModel
from core.studio import brand_name, product_visuals
from core.tools import Tool, ToolContext, ToolOutput
from core.workspace import new_id, slugify

ACTOR = "STUDIO"
VIDEO_SIZES = {"story": (1080, 1920), "square": (1080, 1080), "wide": (1920, 1080)}


class Scene(StrictModel):
    caption: str = Field(min_length=1, max_length=110, description="short on-screen text")
    narration: str = Field(default="", max_length=350, description="what the voiceover says")
    visual: Literal["product", "photo", "brand"] = Field(
        default="product", description="product = our product images; photo = AI photo; "
                                       "brand = branded background")
    photo_prompt: str = Field(default="", max_length=300,
                              description="for visual=photo: describe the photo, no text in it")
    seconds: float = Field(default=4, ge=2, le=10)


class VideoParams(StrictModel):
    title: str = Field(min_length=3, max_length=80)
    format: Literal["story", "square", "wide"] = Field(
        default="story", description="story = 9:16 Reels/TikTok/Shorts, square = feed, wide = 16:9")
    theme: ThemeName = "modern"
    scenes: list[Scene] = Field(min_length=2, max_length=8)
    cta: str = Field(default="", max_length=40, description="call to action on the end card")
    voiceover: bool = True


class VideoSettings(StrictModel):
    fps: int = Field(default=30, ge=15, le=60)
    crf: int = Field(default=21, ge=14, le=32)
    preset: Literal["ultrafast", "veryfast", "faster", "fast", "medium"] = "veryfast"
    max_total_seconds: float = Field(default=60, ge=5, le=180)
    end_card_seconds: float = Field(default=3.0, ge=0, le=8)
    transition_seconds: float = Field(default=0.4, ge=0, le=1.5)
    zoom: float = Field(default=0.08, ge=0, le=0.3)
    timeout_seconds: int = Field(default=1200, ge=60, le=7200)
    voice: tts.VoiceSettings = Field(default_factory=tts.VoiceSettings)


def image_engine_for(company):
    tool = company.tools.tools.get("image_studio") if company is not None else None
    if tool is None or not tool.config.enabled:
        return None
    return tool.engine(company)


def compose_video(company, project_id: str | None, task_id: str | None, params: VideoParams,
                  settings: VideoSettings, allowed_hosts: list[str],
                  group_id: str | None = None) -> tuple[dict, float, list[str]]:
    """Render one video. Returns (video asset, cost EUR, notes). Raises ToolError."""
    ffmpeg = engines.ffmpeg_exe()
    if not ffmpeg:
        raise ToolError("ffmpeg is not available (pip install imageio-ffmpeg)")
    t = theme(params.theme)
    W, H = VIDEO_SIZES[params.format]
    fps, trans = settings.fps, settings.transition_seconds
    notes: list[str] = []
    cost = 0.0
    brand = brand_name(company)
    visuals = [img for _, img in product_visuals(company, project_id)]
    engine = image_engine_for(company)
    use_voice = params.voiceover and tts.availability(settings.voice)[0]
    if params.voiceover and not use_voice:
        notes.append(f"no voiceover ({tts.availability(settings.voice)[1]})")
    group_id = group_id or new_id("vid")
    folder = company.workspace.dir_for(project_id, "videos")
    stem = f"{slugify(params.title, 40)}-{params.format}-{group_id[-6:]}"

    with tempfile.TemporaryDirectory(prefix="video_") as tmp:
        tmp = Path(tmp)
        # 1. visuals + voice per scene (code decides which image each scene gets)
        scenes, product_i = [], 0
        for i, sc in enumerate(params.scenes):
            img, framed = None, False
            if sc.visual == "photo" and sc.photo_prompt.strip() and engine and engine.provider():
                try:
                    img, provider, c_img = engine.generate(sc.photo_prompt, (W, H))
                    cost += c_img
                except ToolError as e:
                    notes.append(f"scene {i + 1}: no AI photo ({e})")
            if img is None and sc.visual != "brand" and visuals:
                img, framed = visuals[product_i % len(visuals)], True
                product_i += 1
            if img is None:
                img = graphics.decorative_background(t, (W, H), seed=i, dark=True)
            clip, clip_len = None, 0.0
            if use_voice and sc.narration.strip():
                clip = tmp / f"voice-{i}.wav"
                try:
                    clip_len = tts.synthesize(sc.narration, settings.voice, clip, allowed_hosts)
                    cost += tts.estimate_cost(settings.voice, len(sc.narration))
                except ToolError as e:
                    notes.append(f"scene {i + 1}: voice failed ({e})")
                    clip = None
            dur = max(sc.seconds, clip_len + 0.35 + trans) if clip else sc.seconds
            scenes.append({"base": _prepare(img, (W, H), settings.zoom, framed, t, i),
                           "overlay": graphics.caption_overlay(t, (W, H), sc.caption),
                           "dur": dur, "clip": clip, "caption": sc.caption,
                           "narration": sc.narration})
        if settings.end_card_seconds > 0:
            card = graphics.end_card(t, (W, H), params.title, params.cta, brand)
            scenes.append({"base": _prepare(card, (W, H), settings.zoom / 2, False, t, 99),
                           "overlay": None, "dur": settings.end_card_seconds, "clip": None,
                           "caption": params.cta or params.title, "narration": ""})
        # cap the length
        total = sum(s["dur"] for s in scenes) - trans * (len(scenes) - 1)
        if total > settings.max_total_seconds:
            factor = settings.max_total_seconds / total
            for s in scenes:
                s["dur"] = max(1.5, s["dur"] * factor)
            notes.append(f"shortened to {settings.max_total_seconds:g}s")
        starts, t0 = [], 0.0
        for s in scenes:
            starts.append(t0)
            t0 += s["dur"] - trans
        total = starts[-1] + scenes[-1]["dur"]

        # 2. audio track
        audio = None
        if any(s["clip"] for s in scenes):
            audio = tmp / "track.wav"
            tts.build_track([(st + 0.15, s["clip"]) for st, s in zip(starts, scenes)],
                              total, audio)

        # 3. frames -> ffmpeg
        out = folder / f"{stem}.mp4"
        args = [ffmpeg, "-y", "-loglevel", "error", "-f", "rawvideo", "-pix_fmt", "rgb24",
                "-s", f"{W}x{H}", "-r", str(fps), "-i", "pipe:0"]
        if audio:
            args += ["-i", str(audio), "-map", "0:v", "-map", "1:a", "-c:a", "aac", "-b:a", "160k"]
        args += ["-c:v", "libx264", "-preset", settings.preset, "-crf", str(settings.crf),
                 "-pix_fmt", "yuv420p", "-movflags", "+faststart", "-shortest", str(out)]
        errlog = tmp / "ffmpeg.log"
        poster = None
        with open(errlog, "wb") as err:
            proc = subprocess.Popen(args, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                                    stderr=err, env=engines.subprocess_env(),
                                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            try:
                n_frames = int(round(total * fps))
                for f in range(n_frames):
                    tt = f / fps
                    frame = _frame_at(tt, scenes, starts, trans, (W, H), settings.zoom)
                    if poster is None and tt >= min(1.0, scenes[0]["dur"] / 2):
                        poster = frame.copy()
                    proc.stdin.write(frame.tobytes())
                proc.stdin.close()
                proc.wait(timeout=settings.timeout_seconds)
            except (BrokenPipeError, OSError, subprocess.TimeoutExpired) as e:
                proc.kill()
                raise ToolError(f"video encoding failed: {type(e).__name__}: "
                                f"{errlog.read_text(errors='replace')[-200:]}") from e
        if proc.returncode != 0 or not out.is_file():
            raise ToolError(f"ffmpeg failed: {errlog.read_text(errors='replace')[-200:]}")

    poster_path = graphics.save_jpg(poster or scenes[0]["base"], folder / f"{stem}-poster.jpg")
    srt = folder / f"{stem}.srt"
    srt.write_text(_srt(scenes, starts, trans), encoding="utf-8")
    meta = {"format": params.format, "seconds": round(total, 1), "scenes": len(params.scenes),
            "voiceover": bool(audio), "voice_engine": settings.voice.engine if audio else None,
            "fps": fps, "size": f"{W}x{H}", "title": params.title, "notes": notes}
    asset = company.assets.register(out, "video", params.title, "video_studio", project_id,
                                    task_id, group_id, meta, thumb_from=poster_path)
    company.assets.register(srt, "captions", f"{params.title} - subtitles", "video_studio",
                            project_id, task_id, group_id, {"video": asset["id"]})
    return asset, cost, notes


def _prepare(img: Image.Image, size, zoom: float, framed: bool, t, seed: int) -> Image.Image:
    """Scene base image, larger than the frame by `zoom` for the Ken Burns move. Product
    pages (portrait documents) are shown whole on a branded background, not cropped."""
    W, H = size
    big = (int(W * (1 + zoom)), int(H * (1 + zoom)))
    if framed and abs(img.width / img.height - W / H) > 0.03:
        canvas = graphics.decorative_background(t, big, seed=seed)
        item = graphics.contain_fit(img, (int(big[0] * 0.84), int(big[1] * 0.72)))
        graphics.paste_with_shadow(canvas, item, ((big[0] - item.width) // 2,
                                                  int(big[1] * 0.1)), blur=max(10, W // 80))
        return canvas
    return graphics.cover_fit(img, big)


def _render_scene(s: dict, p: float, size, zoom: float, direction: int) -> Image.Image:
    W, H = size
    base = s["base"]
    grow = p if direction > 0 else 1 - p          # zoom in on even scenes, out on odd
    scale = 1 + zoom * (1 - grow)
    cw, ch = base.width / scale, base.height / scale
    cw, ch = max(W * 0.5, min(cw, base.width)), max(H * 0.5, min(ch, base.height))
    x = (base.width - cw) / 2
    y = (base.height - ch) / 2
    frame = base.resize(size, Image.Resampling.BILINEAR, box=(x, y, x + cw, y + ch))
    if s["overlay"] is not None:
        frame = Image.alpha_composite(frame.convert("RGBA"), s["overlay"]).convert("RGB")
    return frame


def _frame_at(tt: float, scenes, starts, trans, size, zoom) -> Image.Image:
    i = max(k for k, st in enumerate(starts) if st <= tt + 1e-9)
    s = scenes[i]
    p = min(1.0, (tt - starts[i]) / s["dur"])
    frame = _render_scene(s, p, size, zoom, 1 if i % 2 == 0 else -1)
    if i > 0 and trans > 0 and tt - starts[i] < trans:        # crossfade from previous
        prev = scenes[i - 1]
        pp = min(1.0, (tt - starts[i - 1]) / prev["dur"])
        before = _render_scene(prev, pp, size, zoom, 1 if (i - 1) % 2 == 0 else -1)
        frame = Image.blend(before, frame, (tt - starts[i]) / trans)
    return frame


def _srt(scenes, starts, trans) -> str:
    def ts(x):
        ms = int(round(x * 1000))
        return f"{ms // 3600000:02d}:{ms // 60000 % 60:02d}:{ms // 1000 % 60:02d},{ms % 1000:03d}"
    out = []
    for n, (st, s) in enumerate(zip(starts, scenes), start=1):
        text = s["narration"].strip() or s["caption"]
        out.append(f"{n}\n{ts(st)} --> {ts(st + s['dur'] - trans / 2)}\n{text}\n")
    return "\n".join(out)


class VideoTool(Tool):
    """Short marketing videos from the project's product visuals / AI photos."""
    name = "video_studio"
    Params = VideoParams
    Settings = VideoSettings

    def availability(self) -> tuple[bool, str]:
        if not self.config.enabled:
            return False, "disabled in config"
        return (True, "ok") if engines.ffmpeg_exe() else (False, "ffmpeg not installed")

    def max_cost(self, params: VideoParams, ctx: ToolContext | None = None) -> float:
        photos = sum(1 for s in params.scenes if s.visual == "photo" and s.photo_prompt.strip())
        chars = sum(len(s.narration) for s in params.scenes) if params.voiceover else 0
        engine = image_engine_for(ctx.company) if ctx and ctx.company else None
        per_photo = engine.max_cost_per_image() if engine else 0.0
        return photos * per_photo + tts.estimate_cost(self.settings.voice, chars)

    def run(self, params: VideoParams, ctx: ToolContext) -> ToolOutput:
        asset, cost, notes = compose_video(ctx.company, ctx.project_id, ctx.task_id, params,
                                           self.settings, self.config.allowed_hosts)
        ctx.log(ACTOR, "Created video", asset=asset["id"], seconds=asset["meta"]["seconds"],
                format=params.format, voiceover=asset["meta"]["voiceover"], notes=notes)
        return ToolOutput(summary={"video": asset["id"], "seconds": asset["meta"]["seconds"],
                                   "format": params.format, "notes": notes},
                          assets=[asset], cost_eur=cost,
                          text="\n".join(ctx.company.assets.summary_lines([asset]) + notes))
