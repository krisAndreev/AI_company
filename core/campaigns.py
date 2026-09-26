"""
Marketing campaigns: a dated content plan across channels plus every asset needed to
run it - post images, short videos, captions with hashtags, an email sequence, a blog
article - bundled into a campaign pack (calendar CSV + files) for the owner.

AI proposes, code validates:
  - CampaignBrief (tool params): goal, audience, message, channels, duration
  - CampaignStrategy: angle, content pillars, hooks
  - one ChannelPlan per social channel, validated against that channel's REAL limits
    (caption length incl. hashtags, hashtag count; code-owned table below)
  - EmailSequence and BlogPost when those channels are chosen
  - a video storyboard (VideoParams) for up to `videos` posts
Code picks dates, sizes, which product images to show, renders everything and
registers all files as DRAFT assets. Posting stays manual (the 'publishing'
capability is a human step) - nothing is published by the system.
"""

import csv
import io
import zipfile
from datetime import timedelta
from typing import Annotated, ClassVar, Literal

from pydantic import Field, field_validator, model_validator

from core.design import ThemeName
from core.imagegen import ImageParams, create_images
from core.net import ToolError
from core.schemas import StrictModel
from core.structured import StructuredOutputError
from core.studio import latest_product
from core.tasks import utcnow
from core.tools import Tool, ToolContext, ToolOutput
from core.video import VideoParams, compose_video, image_engine_for
from core.workspace import new_id, slugify

ACTOR = "STUDIO"
Channel = Literal["instagram", "facebook", "pinterest", "tiktok", "x", "email", "blog"]
SOCIAL = ("instagram", "facebook", "pinterest", "tiktok", "x")

# Real platform limits and the image size used per channel (code-owned).
CHANNEL_RULES: dict[str, dict] = {
    "instagram": {"max_chars": 2200, "max_hashtags": 30, "format": "portrait", "video": "story",
                  "cta": "Link in bio"},
    "facebook": {"max_chars": 2000, "max_hashtags": 5, "format": "square", "video": "square",
                 "cta": "Shop now"},
    "pinterest": {"max_chars": 500, "max_hashtags": 8, "format": "pin", "video": "story",
                  "cta": "Get it now", "needs_title": True},
    "tiktok": {"max_chars": 2200, "max_hashtags": 8, "format": "story", "video": "story",
               "cta": "Link in bio"},
    "x": {"max_chars": 280, "max_hashtags": 3, "format": "landscape", "video": "wide",
          "cta": "Shop now"},
}


class CampaignBrief(StrictModel):
    name: str = Field(min_length=3, max_length=80)
    goal: str = Field(min_length=5, max_length=300, description="e.g. first 20 sales in 2 weeks")
    audience: str = Field(min_length=3, max_length=200)
    key_message: str = Field(min_length=5, max_length=300)
    offer: str = Field(default="", max_length=120, description="e.g. 20% off launch week")
    channels: list[Channel] = Field(min_length=1, max_length=6)
    duration_days: int = Field(default=14, ge=3, le=30)
    posts_per_channel: int = Field(default=3, ge=1, le=6)
    videos: int = Field(default=1, ge=0, le=3, description="short videos to produce")
    theme: ThemeName = "modern"

    @field_validator("channels")
    @classmethod
    def _unique(cls, v):
        return list(dict.fromkeys(v))


class CampaignStrategy(StrictModel):
    angle: str = Field(min_length=10, max_length=400)
    pillars: list[Annotated[str, Field(max_length=120)]] = Field(min_length=2, max_length=4)
    hooks: list[Annotated[str, Field(max_length=100)]] = Field(min_length=3, max_length=8)
    kpis: list[Annotated[str, Field(max_length=120)]] = Field(min_length=2, max_length=5)


class PostDraft(StrictModel):
    day: int = Field(ge=1, le=30, description="campaign day to post on")
    hook: str = Field(min_length=3, max_length=90, description="short headline shown ON the image")
    caption: str = Field(min_length=10, max_length=2200)
    hashtags: list[Annotated[str, Field(max_length=40)]] = Field(default_factory=list, max_length=30)
    visual: Literal["product", "photo", "quote", "video"] = "product"
    photo_prompt: str = Field(default="", max_length=300,
                              description="for visual=photo: the photo to generate, no text in it")
    title: str = Field(default="", max_length=100, description="pin title (Pinterest only)")

    @field_validator("hashtags")
    @classmethod
    def _tags(cls, v):
        out = []
        for tag in v:
            tag = "".join(ch for ch in tag.strip().lstrip("#") if ch.isalnum() or ch == "_")
            if tag and tag.lower() not in (t.lower() for t in out):
                out.append(tag)
        return out


class ChannelPlan(StrictModel):
    RULES: ClassVar[dict] = {}
    posts: list[PostDraft] = Field(min_length=1, max_length=6)

    @model_validator(mode="after")
    def _limits(self):
        r = self.RULES
        for i, p in enumerate(self.posts, start=1):
            full = len(p.caption) + sum(len(t) + 2 for t in p.hashtags)
            if r and full > r["max_chars"]:
                raise ValueError(f"post {i}: caption plus hashtags is {full} characters, the "
                                 f"limit is {r['max_chars']}")
            if r and len(p.hashtags) > r["max_hashtags"]:
                raise ValueError(f"post {i}: {len(p.hashtags)} hashtags, max {r['max_hashtags']}")
            if r.get("needs_title") and not p.title.strip():
                raise ValueError(f"post {i}: a pin title is required")
        return self


def plan_schema(channel: str) -> type[ChannelPlan]:
    return type(f"{channel.title()}Plan", (ChannelPlan,),
                {"__annotations__": {"RULES": ClassVar[dict]}, "RULES": CHANNEL_RULES[channel],
                 "__module__": __name__})


class EmailDraft(StrictModel):
    day: int = Field(ge=1, le=30)
    subject: str = Field(min_length=5, max_length=80)
    preview: str = Field(default="", max_length=120)
    body: str = Field(min_length=80, max_length=3000)


class EmailSequence(StrictModel):
    emails: list[EmailDraft] = Field(min_length=1, max_length=5)


class BlogPost(StrictModel):
    title: str = Field(min_length=10, max_length=90)
    meta_description: str = Field(min_length=50, max_length=160)
    body: str = Field(min_length=600, max_length=9000, description="markdown with ## headings")


class CampaignSettings(StrictModel):
    start_offset_days: int = Field(default=1, ge=0, le=30)
    max_ai_photos: int = Field(default=6, ge=0, le=30)


class CampaignTool(Tool):
    name = "campaign_builder"
    Params = CampaignBrief
    Settings = CampaignSettings
    needs_model = True

    def max_cost(self, brief: CampaignBrief, ctx: ToolContext | None = None) -> float:
        engine = image_engine_for(ctx.company) if ctx and ctx.company else None
        per = engine.max_cost_per_image() if engine else 0.0
        return per * self.settings.max_ai_photos

    def run(self, brief: CampaignBrief, ctx: ToolContext) -> ToolOutput:
        c = ctx.company
        campaign_id = new_id("camp")
        start = (utcnow() + timedelta(days=self.settings.start_offset_days)).date()
        product = latest_product(c, ctx.project_id)
        listing = (product or {}).get("listing") or {}
        product_text = (f"Product: {product['title']}\n" + (
            f"Listing: {listing.get('title', '')}\n{listing.get('description', '')[:600]}\n"
            if listing else "")) if product else "No product file exists yet; promote the offer.\n"
        head = (f"Campaign: {brief.name}\nGoal: {brief.goal}\nAudience: {brief.audience}\n"
                f"Key message: {brief.key_message}\n" + (f"Offer: {brief.offer}\n" if brief.offer else "")
                + product_text
                + (f"Earlier work:\n{ctx.context[:1500]}\n" if ctx.context else ""))
        ctx.log(ACTOR, "Building campaign", campaign=campaign_id, channels=brief.channels,
                days=brief.duration_days)
        warnings: list[str] = []

        strategy = ctx.generate(
            f"{head}\nDefine the campaign strategy: one clear angle, 2-4 content pillars, "
            f"3-8 scroll-stopping hooks, and 2-5 KPIs to track.", CampaignStrategy,
            system="You are a pragmatic organic-marketing strategist for small online shops. "
                   "No fake scarcity, fake reviews or unverifiable claims.")
        strat_text = (f"Angle: {strategy.angle}\nPillars: {'; '.join(strategy.pillars)}\n"
                      f"Hooks: {'; '.join(strategy.hooks)}\n")

        posts: list[dict] = []
        for ch in [x for x in brief.channels if x in SOCIAL]:
            rules = CHANNEL_RULES[ch]
            try:
                plan = ctx.generate(
                    f"{head}{strat_text}\nWrite exactly {brief.posts_per_channel} {ch} posts spread "
                    f"over days 1-{brief.duration_days}. Limits: caption + hashtags at most "
                    f"{rules['max_chars']} characters, at most {rules['max_hashtags']} hashtags"
                    + (", every post needs a pin title" if rules.get("needs_title") else "")
                    + ". hook = the short headline printed on the image. visual: product "
                      "(show our product), photo (lifestyle photo - give photo_prompt), quote "
                      "(text card) or video. Write in the native style of the platform.",
                    plan_schema(ch), system="You write native, honest social media content.",
                    temperature=0.6)
            except StructuredOutputError:
                warnings.append(f"{ch}: posts could not be generated")
                continue
            for p in plan.posts[: brief.posts_per_channel]:
                posts.append({"channel": ch, "draft": p, "day": min(p.day, brief.duration_days)})

        emails, blog = None, None
        if "email" in brief.channels:
            try:
                emails = ctx.generate(
                    f"{head}{strat_text}\nWrite a {min(4, max(2, brief.duration_days // 4))}-email "
                    f"sequence over days 1-{brief.duration_days} for people who signed up. Plain, "
                    f"friendly text; one clear call to action per email; include an unsubscribe "
                    f"reminder line at the end.", EmailSequence,
                    system="You write helpful, non-spammy marketing emails.", temperature=0.5)
            except StructuredOutputError:
                warnings.append("email sequence could not be generated")
        if "blog" in brief.channels:
            try:
                blog = ctx.generate(
                    f"{head}{strat_text}\nWrite an SEO blog article (600-1200 words, markdown with "
                    f"## headings) that genuinely helps the audience and naturally mentions the "
                    f"product once near the end.", BlogPost,
                    system="You write useful, search-friendly articles.", temperature=0.5)
            except StructuredOutputError:
                warnings.append("blog article could not be generated")

        # --- assets ---------------------------------------------------------------------------
        engine = image_engine_for(c)
        photos_left = self.settings.max_ai_photos if engine and engine.provider() else 0
        videos_left, cost, assets = brief.videos, 0.0, []
        posts.sort(key=lambda p: (p["day"], p["channel"]))
        for n, post in enumerate(posts, start=1):
            d, ch = post["draft"], post["channel"]
            rules = CHANNEL_RULES[ch]
            post["date"] = (start + timedelta(days=post["day"] - 1)).isoformat()
            post["files"] = []
            if d.visual == "video" and videos_left > 0:
                try:
                    board = ctx.generate(
                        f"{head}\nStoryboard a {rules['video']} video for {ch}: hook \"{d.hook}\". "
                        f"Caption: {d.caption[:400]}\n3-6 scenes, 15-35 seconds in total. Scene 1 "
                        f"must hook in 2 seconds. Use visual=product for scenes showing our "
                        f"product. Keep captions short; narration is spoken aloud.",
                        VideoParams, system="You storyboard short vertical marketing videos.",
                        temperature=0.5)
                    board = board.model_copy(update={"format": rules["video"], "theme": brief.theme,
                                                     "cta": board.cta or rules["cta"]})
                    tool = c.tools.tools.get("video_studio")
                    if tool is None or not tool.availability()[0]:
                        raise ToolError("video tool unavailable")
                    video, v_cost, v_notes = compose_video(c, ctx.project_id, ctx.task_id, board,
                                                           tool.settings, tool.config.allowed_hosts,
                                                           campaign_id)
                    cost += v_cost
                    videos_left -= 1
                    warnings += [f"video {n}: {x}" for x in v_notes]
                    assets.append(video)
                    post["files"].append(video)
                    continue
                except (StructuredOutputError, ToolError) as e:
                    warnings.append(f"post {n} ({ch}): video skipped ({str(e)[:100]}); made an image")
            layout = {"product": "product", "quote": "quote", "photo": "photo",
                      "video": "product"}[d.visual]
            prompt = d.photo_prompt if (layout == "photo" and photos_left > 0) else ""
            if layout == "photo" and not prompt:
                layout = "product"
            imgs, i_cost, i_notes, _ = create_images(
                c, ctx.project_id, ctx.task_id, ImageParams(
                    purpose=f"{ch} post day {post['day']}", format=rules["format"], layout=layout,
                    headline=d.hook, subline=brief.offer[:160], cta=rules["cta"],
                    photo_prompt=prompt, theme=brief.theme), engine, campaign_id,
                f"campaigns/{slugify(brief.name, 30)}-{campaign_id[-6:]}/images",
                file_stem=f"{post['date']}-{ch}", extra_meta={"channel": ch, "date": post["date"]})
            if prompt:
                photos_left -= 1
            cost += i_cost
            warnings += [f"post {n} ({ch}): {x}" for x in i_notes]
            assets += imgs
            post["files"] += imgs

        pack_dir = c.workspace.dir_for(ctx.project_id, "campaigns",
                                       f"{slugify(brief.name, 30)}-{campaign_id[-6:]}")
        blog_img = None
        if blog:
            imgs, i_cost, _, _ = create_images(
                c, ctx.project_id, ctx.task_id, ImageParams(
                    purpose="blog featured image", format="landscape", layout="product",
                    headline=blog.title, theme=brief.theme), engine, campaign_id,
                f"campaigns/{slugify(brief.name, 30)}-{campaign_id[-6:]}/images", file_stem="blog")
            blog_img = imgs[0]
            assets += imgs
        written = self._write_pack(c, pack_dir, brief, strategy, posts, emails, blog, blog_img,
                                   start, warnings)
        for path, kind, title in written["files"]:
            assets.append(c.assets.register(path, kind, title, "campaign_builder", ctx.project_id,
                                            ctx.task_id, campaign_id,
                                            {"campaign": brief.name}))
        first_image = next((a for a in assets if a["mime"].startswith("image/")), None)
        pack = c.assets.register(written["zip"], "pack", f"{brief.name} - campaign pack",
                                 "campaign_builder", ctx.project_id, ctx.task_id, campaign_id,
                                 {"campaign": brief.name, "posts": len(posts),
                                  "start": start.isoformat(), "channels": brief.channels,
                                  "warnings": warnings},
                                 thumb_from=c.assets.file_path(first_image["id"])
                                 if first_image else None)
        assets.append(pack)
        ctx.log(ACTOR, "Built campaign", campaign=campaign_id, posts=len(posts),
                emails=len(emails.emails) if emails else 0, blog=bool(blog),
                files=len(assets), warnings=warnings[:10])
        summary = {"campaign_id": campaign_id, "posts": len(posts), "start": start.isoformat(),
                   "channels": brief.channels, "emails": len(emails.emails) if emails else 0,
                   "blog": bool(blog), "videos": brief.videos - videos_left,
                   "warnings": warnings[:10]}
        text = (f"Campaign '{brief.name}' ({start.isoformat()}, {brief.duration_days} days): "
                f"{len(posts)} posts on {', '.join(brief.channels)}. Angle: {strategy.angle}\n"
                + "\n".join(f"- {p['date']} {p['channel']}: {p['draft'].hook}" for p in posts[:12])
                + f"\nPack: {pack['path']}" + ("\nWarnings: " + "; ".join(warnings[:5]) if warnings else ""))
        return ToolOutput(summary=summary, assets=assets, cost_eur=cost, text=text)

    def _write_pack(self, c, folder, brief, strategy, posts, emails, blog, blog_img, start,
                    warnings) -> dict:
        files = []
        root = c.workspace.root

        def rel(asset):   # path inside the zip pack
            src = root / asset["path"]
            return src.relative_to(folder).as_posix() if src.is_relative_to(folder) \
                else f"media/{src.name}"

        cal = io.StringIO()
        w = csv.writer(cal)
        w.writerow(["date", "channel", "type", "title", "caption", "hashtags", "media"])
        for p in posts:
            d = p["draft"]
            w.writerow([p["date"], p["channel"], "video" if any(f["kind"] == "video" for f in p["files"])
                        else "image", d.title or d.hook, d.caption,
                        " ".join("#" + t for t in d.hashtags), ";".join(rel(f) for f in p["files"])])
        if emails:
            for e in emails.emails:
                w.writerow([(start + timedelta(days=e.day - 1)).isoformat(), "email", "email",
                            e.subject, e.preview, "", f"emails/day-{e.day:02d}.md"])
        (folder / "calendar.csv").write_text(cal.getvalue(), encoding="utf-8")
        files.append((folder / "calendar.csv", "copy", f"{brief.name} - content calendar"))

        md = [f"# {brief.name}", "", f"**Goal:** {brief.goal}  ", f"**Audience:** {brief.audience}  ",
              f"**Message:** {brief.key_message}  "]
        if brief.offer:
            md.append(f"**Offer:** {brief.offer}  ")
        md += [f"**Runs:** {start.isoformat()} for {brief.duration_days} days  ", "",
               "## Strategy", "", strategy.angle, "", "**Pillars:** " + "; ".join(strategy.pillars),
               "", "**Hooks:** " + "; ".join(strategy.hooks), "", "## Track these KPIs", ""]
        md += [f"- [ ] {k}" for k in strategy.kpis] + ["", "## Posts", ""]
        for p in posts:
            d = p["draft"]
            md += [f"### {p['date']} - {p['channel']}" + (f": {d.title}" if d.title else ""), "",
                   f"**On image:** {d.hook}", "", d.caption, "",
                   " ".join("#" + t for t in d.hashtags), "",
                   "Media: " + ", ".join(rel(f) for f in p["files"]), ""]
        if emails:
            (folder / "emails").mkdir(exist_ok=True)
            md += ["## Email sequence", ""]
            for e in emails.emails:
                path = folder / "emails" / f"day-{e.day:02d}.md"
                path.write_text(f"Subject: {e.subject}\nPreview: {e.preview}\n\n{e.body}\n",
                                encoding="utf-8")
                files.append((path, "copy", f"Email day {e.day}: {e.subject}"))
                md.append(f"- Day {e.day}: **{e.subject}** (emails/day-{e.day:02d}.md)")
            md.append("")
        if blog:
            (folder / "blog").mkdir(exist_ok=True)
            path = folder / "blog" / f"{slugify(blog.title, 60)}.md"
            path.write_text(f"---\ntitle: \"{blog.title}\"\ndescription: \"{blog.meta_description}\"\n"
                            + (f"image: {rel(blog_img)}\n" if blog_img else "") + f"---\n\n"
                            f"# {blog.title}\n\n{blog.body}\n", encoding="utf-8")
            files.append((path, "copy", f"Blog: {blog.title}"))
            md += ["## Blog article", "", f"[{blog.title}](blog/{path.name})", ""]
        if warnings:
            md += ["## Review notes", ""] + [f"- {x}" for x in warnings] + [""]
        md += ["---", "Nothing has been published. Review, approve and post these yourself "
                      "(or schedule them in your social media tool)."]
        (folder / "campaign.md").write_text("\n".join(md) + "\n", encoding="utf-8")
        files.append((folder / "campaign.md", "copy", f"{brief.name} - campaign plan"))

        zpath = folder / f"{slugify(brief.name, 40)}-pack.zip"
        with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED) as z:
            for p in folder.rglob("*"):
                if p.is_file() and p != zpath:
                    z.write(p, p.relative_to(folder).as_posix())
            for p in posts:                       # media stored outside the pack folder
                for f in p["files"]:
                    src = root / f["path"]
                    if not src.is_relative_to(folder):
                        z.write(src, f"media/{src.name}")
        return {"files": files, "zip": zpath}
