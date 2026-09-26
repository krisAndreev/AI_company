"""
Images: marketing graphics (always available, drawn by code) and AI photos from a
provider chosen by code in config order:

  sdcpp         stable-diffusion.cpp on this PC (free). Holds the node's GPU slot and
                unloads Ollama first: the 4 GB card never runs two models at once.
  a1111         an AUTOMATIC1111 / Forge / SD.Next / sd-server WebUI API (local or LAN)
  openai        OpenAI-compatible images API (API key from env)
  pollinations  Pollinations (OpenAI-compatible, API key from env)

The model proposes WHAT to show (headline, layout, a photo description); code
decides provider, size, style and safety filtering, and renders the final image.
"""

import base64
import io
import random
import re
import tempfile
import time
from pathlib import Path
from typing import Literal

from PIL import Image
from pydantic import Field

from core import engines, graphics, net
from core.design import FORMATS, FormatName, ThemeName, theme
from core.net import ToolError
from core.resources import gpu_session
from core.schemas import StrictModel
from core.studio import brand_name, product_visuals
from core.tools import Tool, ToolContext, ToolOutput
from core.workspace import new_id, slugify

ACTOR = "STUDIO"
Size = tuple[int, int]
_BLOCKED = re.compile(r"\b(nude|nudity|naked|nsfw|sexual|sexy|erotic|porn\w*|lingerie|gore|"
                      r"blood|weapon|guns?|rifle|celebrity|trademark)\b", re.I)


def clean_prompt(text: str, limit: int = 400) -> str:
    text = re.sub(r"[\x00-\x1f\x7f]+", " ", text)
    text = re.sub(r"<[^>]*>", " ", text)          # no embedded engine directives
    text = re.sub(r"\s+", " ", text).strip().lstrip("-").strip()
    return text[:limit]


def prompt_problem(text: str) -> str | None:
    m = _BLOCKED.search(text)
    return f"photo prompt refused (contains {m.group(0)!r})" if m else None


def closest_size(target: Size, sizes: list[Size]) -> Size:
    ratio = target[0] / target[1]
    return min(sizes, key=lambda s: abs(s[0] / s[1] - ratio))


# --- provider settings -------------------------------------------------------------------------

_SDXL_SIZES = [(1024, 1024), (832, 1216), (1216, 832), (896, 1152), (1152, 896), (768, 1344),
               (1344, 768)]


class SdCppSettings(StrictModel):
    enabled: bool = True
    exe: str = "tools/sdcpp/sd-cli.exe"
    model: str = "models/image/sdxl_lightning_4step.q4_0.gguf"
    diffusion_model: str = ""
    vae: str = ""
    llm: str = ""
    clip_l: str = ""
    clip_g: str = ""
    t5xxl: str = ""
    steps: int = Field(default=4, ge=1, le=60)
    cfg_scale: float = Field(default=1.0, ge=0, le=20)
    sampling_method: Literal["euler", "euler_a", "dpm++2m", "lcm", "ddim_trailing", "tcd",
                             "res_multistep"] = "euler"
    scheduler: Literal["discrete", "karras", "exponential", "sgm_uniform", "simple",
                       "smoothstep"] = "sgm_uniform"
    native_sizes: list[tuple[int, int]] = Field(default_factory=lambda: list(_SDXL_SIZES))
    flags: list[Literal["--vae-tiling", "--offload-to-cpu", "--diffusion-fa", "--fa",
                        "--clip-on-cpu", "--vae-on-cpu", "--mmap"]] = ["--vae-tiling"]
    node: str = ""
    timeout_seconds: int = Field(default=900, ge=30, le=3600)
    cost_per_image_eur: float = Field(default=0, ge=0)


class A1111Settings(StrictModel):
    enabled: bool = False
    base_url: str = "http://127.0.0.1:7860"
    steps: int = Field(default=20, ge=1, le=80)
    cfg_scale: float = Field(default=6.0, ge=0, le=20)
    sampler_name: str = "DPM++ 2M"
    native_sizes: list[tuple[int, int]] = Field(default_factory=lambda: list(_SDXL_SIZES))
    node: str = ""                 # GPU node to hold while generating (same PC as Ollama)
    timeout_seconds: int = Field(default=600, ge=30, le=3600)
    cost_per_image_eur: float = Field(default=0, ge=0)


class HttpImageSettings(StrictModel):
    enabled: bool = False
    base_url: str
    model: str
    api_key_env: str
    sizes: list[tuple[int, int]]
    quality: str = ""
    timeout_seconds: int = Field(default=240, ge=10, le=900)
    cost_per_image_eur: float = Field(ge=0)


class ImageSettings(StrictModel):
    provider_order: list[Literal["sdcpp", "a1111", "openai", "pollinations"]] = \
        ["sdcpp", "a1111", "openai", "pollinations"]
    style_suffix: str = Field(default="professional photography, natural light, high detail",
                              max_length=300)
    negative_prompt: str = Field(default="text, letters, words, watermark, logo, signature, "
                                         "blurry, lowres, deformed, extra fingers", max_length=400)
    max_images_per_call: int = Field(default=4, ge=1, le=12)
    sdcpp: SdCppSettings = Field(default_factory=SdCppSettings)
    a1111: A1111Settings = Field(default_factory=A1111Settings)
    openai: HttpImageSettings = Field(default_factory=lambda: HttpImageSettings(
        base_url="https://api.openai.com/v1", model="gpt-image-1", api_key_env="OPENAI_API_KEY",
        sizes=[(1024, 1024), (1024, 1536), (1536, 1024)], quality="medium",
        cost_per_image_eur=0.05))
    pollinations: HttpImageSettings = Field(default_factory=lambda: HttpImageSettings(
        base_url="https://gen.pollinations.ai/v1", model="flux",
        api_key_env="POLLINATIONS_API_KEY", sizes=[(1024, 1024), (832, 1216), (1216, 832)],
        cost_per_image_eur=0.01))


# --- providers ------------------------------------------------------------------------------------

class ImageEngine:
    """Generates AI photos with the first available provider. Shared by the image,
    video and campaign tools."""

    def __init__(self, settings: ImageSettings, allowed_hosts: list[str], company=None):
        self.s = settings
        self.allowed_hosts = allowed_hosts
        self.company = company
        self._a1111_ok: tuple[float, bool] = (0.0, False)

    # which providers can run right now
    def status(self) -> list[dict]:
        return [{"provider": p, **dict(zip(("available", "reason"), self._available(p)))}
                for p in self.s.provider_order]

    def provider(self) -> str | None:
        return next((p for p in self.s.provider_order if self._available(p)[0]), None)

    def cost_per_image(self, provider: str | None = None) -> float:
        p = provider or self.provider()
        return getattr(self.s, p).cost_per_image_eur if p else 0.0

    def max_cost_per_image(self) -> float:
        return max([getattr(self.s, p).cost_per_image_eur for p in self.s.provider_order
                    if self._available(p)[0]] or [0.0])

    def _available(self, p: str) -> tuple[bool, str]:
        cfg = getattr(self.s, p)
        if not cfg.enabled:
            return False, "disabled"
        if p == "sdcpp":
            missing = [f for f in [cfg.exe, cfg.model, cfg.diffusion_model, cfg.vae, cfg.llm,
                                   cfg.clip_l, cfg.clip_g, cfg.t5xxl]
                       if f and not engines.resolve(f).is_file()]
            if not (cfg.model or cfg.diffusion_model):
                return False, "no model configured"
            return (False, f"missing {missing[0]}") if missing else (True, "ok")
        if p == "a1111":
            return self._a1111_reachable()
        import os
        if not os.environ.get(cfg.api_key_env):
            return False, f"environment variable {cfg.api_key_env} not set"
        return True, "ok"

    def _a1111_reachable(self) -> tuple[bool, str]:
        ts, ok = self._a1111_ok
        if time.time() - ts < 60:
            return ok, "ok" if ok else "not reachable"
        try:
            net.request("GET", self.s.a1111.base_url.rstrip("/") + "/sdapi/v1/samplers",
                        self.allowed_hosts, timeout=3, retries=0)
            ok = True
        except ToolError:
            ok = False
        self._a1111_ok = (time.time(), ok)
        return ok, "ok" if ok else "not reachable"

    # generation
    def generate(self, prompt: str, size: Size, seed: int | None = None) -> tuple[Image.Image, str, float]:
        """One photo at exactly `size` (cropped from the provider's native size).
        Returns (image, provider, cost). Raises ToolError."""
        provider = self.provider()
        if provider is None:
            raise ToolError("no AI image provider available")
        prompt = clean_prompt(prompt)
        problem = prompt_problem(prompt)
        if problem:
            raise ToolError(problem)
        full = f"{prompt}, {self.s.style_suffix}" if self.s.style_suffix else prompt
        seed = random.randint(1, 2**31 - 1) if seed is None else seed
        img = getattr(self, f"_gen_{provider}")(full, size, seed)
        return graphics.cover_fit(img, size), provider, self.cost_per_image(provider)

    def _pool(self):
        return getattr(self.company, "resources", None) if self.company else None

    def _slot(self, preferred: str):
        pool = self._pool()
        if pool is not None:
            node, size = pool.node_for_slot(preferred or None)
        else:
            node, size = preferred or "local", 1
        return gpu_session(node, size, pool=pool, label="image")

    def _gen_sdcpp(self, prompt: str, size: Size, seed: int) -> Image.Image:
        cfg = self.s.sdcpp
        w, h = closest_size(size, cfg.native_sizes)
        with tempfile.TemporaryDirectory(prefix="sdcpp_") as tmp:
            tmp = Path(tmp)
            (tmp / "prompt.txt").write_text(prompt, encoding="utf-8")
            (tmp / "negative.txt").write_text(self.s.negative_prompt, encoding="utf-8")
            out = tmp / "out.png"
            args = [engines.resolve(cfg.exe)]
            for flag, value in (("-m", cfg.model), ("--diffusion-model", cfg.diffusion_model),
                                ("--vae", cfg.vae), ("--llm", cfg.llm), ("--clip_l", cfg.clip_l),
                                ("--clip_g", cfg.clip_g), ("--t5xxl", cfg.t5xxl)):
                if value:
                    args += [flag, engines.resolve(value)]
            args += ["--prompt-file", tmp / "prompt.txt", "--negative-prompt-file",
                     tmp / "negative.txt", "-W", w, "-H", h, "--steps", cfg.steps,
                     "--cfg-scale", cfg.cfg_scale, "--sampling-method", cfg.sampling_method,
                     "--scheduler", cfg.scheduler, "-s", seed, "-o", out,
                     "--disable-image-metadata", *cfg.flags]
            with self._slot(cfg.node):
                for attempt in (1, 2):
                    try:
                        proc = engines.run_fixed(args, timeout=cfg.timeout_seconds, cwd=tmp)
                    except Exception as e:
                        raise ToolError(f"sd-cli could not run: {type(e).__name__}") from e
                    text = (proc.stdout + proc.stderr).decode("utf-8", "replace").lower()
                    if proc.returncode == 0 or attempt == 2 or "memory" not in text:
                        break
                    # VRAM taken by something outside this process (e.g. a model someone
                    # started in a terminal): unload again, give the driver a moment, retry
                    pool = self._pool()
                    if pool is not None:
                        pool.unload_models(pool.node_for_slot(cfg.node or None)[0])
                    time.sleep(5)
            if proc.returncode != 0 or not out.is_file():
                lines = (proc.stdout + proc.stderr).decode("utf-8", "replace").splitlines()
                errors = [l.strip() for l in lines if "ERROR" in l or "error" in l.lower()][-3:]
                raise ToolError(f"sd-cli failed (exit {proc.returncode}): "
                                f"{' | '.join(errors or lines[-1:])[:300]}")
            with Image.open(out) as im:
                return im.convert("RGB")

    def _gen_a1111(self, prompt: str, size: Size, seed: int) -> Image.Image:
        cfg = self.s.a1111
        w, h = closest_size(size, cfg.native_sizes)
        body = {"prompt": prompt, "negative_prompt": self.s.negative_prompt, "width": w,
                "height": h, "steps": cfg.steps, "cfg_scale": cfg.cfg_scale, "seed": seed,
                "sampler_name": cfg.sampler_name, "batch_size": 1}
        with self._slot(cfg.node):
            data = net.request("POST", cfg.base_url.rstrip("/") + "/sdapi/v1/txt2img",
                               self.allowed_hosts, json_body=body, timeout=cfg.timeout_seconds)
        images = data.get("images") or []
        if not images:
            raise ToolError("image server returned no image")
        return _decode_b64_image(images[0])

    def _gen_openai(self, prompt: str, size: Size, seed: int) -> Image.Image:
        return self._gen_http(self.s.openai, prompt, size)

    def _gen_pollinations(self, prompt: str, size: Size, seed: int) -> Image.Image:
        return self._gen_http(self.s.pollinations, prompt, size, seed)

    def _gen_http(self, cfg: HttpImageSettings, prompt: str, size: Size,
                  seed: int | None = None) -> Image.Image:
        import os
        w, h = closest_size(size, cfg.sizes)
        body = {"model": cfg.model, "prompt": prompt, "n": 1, "size": f"{w}x{h}"}
        if cfg.quality:
            body["quality"] = cfg.quality
        if seed is not None:
            body["seed"] = seed
        data = net.request("POST", cfg.base_url.rstrip("/") + "/images/generations",
                           self.allowed_hosts, json_body=body, timeout=cfg.timeout_seconds,
                           headers={"Authorization": f"Bearer {os.environ.get(cfg.api_key_env, '')}"},
                           retries=1)
        item = (data.get("data") or [{}])[0]
        if item.get("b64_json"):
            return _decode_b64_image(item["b64_json"])
        if item.get("url"):
            raw = net.request("GET", item["url"], self.allowed_hosts, expect="bytes",
                              timeout=cfg.timeout_seconds)
            return Image.open(io.BytesIO(raw)).convert("RGB")
        raise ToolError("image API returned no image")


def _decode_b64_image(data: str) -> Image.Image:
    if "," in data[:100] and data.startswith("data:"):
        data = data.split(",", 1)[1]
    try:
        return Image.open(io.BytesIO(base64.b64decode(data))).convert("RGB")
    except Exception as e:
        raise ToolError(f"could not decode image: {type(e).__name__}") from e


# --- the tool ---------------------------------------------------------------------------------------

def create_images(company, project_id, task_id, params: "ImageParams", engine: ImageEngine | None,
                  group_id: str, folder_name: str, file_stem: str | None = None,
                  extra_meta: dict | None = None) -> tuple[list[dict], float, list[str], list[str]]:
    """Render params.variants images and register them. Returns (assets, cost, notes,
    AI providers used). A failed/refused AI photo falls back to a designed background."""
    t = theme(params.theme)
    size = FORMATS[params.format]
    brand = brand_name(company)
    visuals = product_visuals(company, project_id)
    product = visuals[0][1] if visuals else None
    folder = company.workspace.dir_for(project_id, *folder_name.split("/"))
    assets, notes, cost, providers = [], [], 0.0, set()
    for i in range(params.variants):
        photo, provider = None, None
        if params.photo_prompt.strip() and params.layout in ("photo", "headline", "quote"):
            if engine is None or engine.provider() is None:
                notes.append("no AI image provider available; used a designed background")
            else:
                try:
                    photo, provider, c_img = engine.generate(params.photo_prompt, size)
                    cost += c_img
                    providers.add(provider)
                except ToolError as e:
                    notes.append(f"variant {i + 1}: no AI photo ({e}); used a designed background")
        layout = params.layout
        if layout == "photo" and photo is None:
            layout = "product" if product is not None else "headline"
        img = graphics.social_graphic(t, size, layout, params.headline or params.purpose,
                                      params.subline, params.cta, brand, photo=photo,
                                      product=product, seed=i + len(params.headline))
        stem = file_stem or slugify(params.headline or params.purpose, 40)
        path = graphics.save_png(img, folder / f"{stem}-{params.format}-{group_id[-6:]}-{i + 1}.png")
        assets.append(company.assets.register(
            path, "image", f"{params.headline or params.purpose} ({params.format})", "image_studio",
            project_id, task_id, group_id, meta={
                "format": params.format, "layout": layout, "theme": params.theme,
                "photo_provider": provider, "photo_prompt": params.photo_prompt,
                "purpose": params.purpose, **(extra_meta or {})}))
    return assets, cost, notes, sorted(providers)


class ImageParams(StrictModel):
    purpose: str = Field(min_length=3, max_length=200, description="where the image will be used")
    format: FormatName = Field(default="square", description="square, portrait, story, pin, "
                                                             "landscape, listing, thumbnail")
    layout: Literal["product", "headline", "quote", "photo"] = Field(
        default="product", description="product = our product on a branded background; photo "
                                       "= AI photo with text; headline = bold text; quote = quote card")
    headline: str = Field(default="", max_length=90)
    subline: str = Field(default="", max_length=160)
    cta: str = Field(default="", max_length=28, description="short call to action")
    photo_prompt: str = Field(default="", max_length=400,
                              description="describe a photo WITHOUT any text in it (optional)")
    theme: ThemeName = "modern"
    variants: int = Field(default=1, ge=1, le=3)


class ImageTool(Tool):
    """Marketing images: branded graphics, product images and AI photos."""
    name = "image_studio"
    Params = ImageParams
    Settings = ImageSettings

    def engine(self, company=None) -> ImageEngine:
        return ImageEngine(self.settings, self.config.allowed_hosts, company)

    def max_cost(self, params: ImageParams, ctx: ToolContext | None = None) -> float:
        if not params.photo_prompt.strip():
            return 0.0
        return params.variants * self.engine().max_cost_per_image()

    def run(self, params: ImageParams, ctx: ToolContext) -> ToolOutput:
        c = ctx.company
        assets, cost, notes, providers = create_images(
            c, ctx.project_id, ctx.task_id, params, self.engine(c), new_id("img"), "images")
        ctx.log(ACTOR, "Created images", count=len(assets), format=params.format,
                layout=params.layout, providers=providers, notes=notes)
        return ToolOutput(summary={"images": len(assets), "format": params.format,
                                   "ai_photos": providers, "notes": notes},
                          assets=assets, cost_eur=cost,
                          text="\n".join(c.assets.summary_lines(assets) + notes))

    def availability(self) -> tuple[bool, str]:
        return (True, "ok") if self.config.enabled else (False, "disabled in config")
