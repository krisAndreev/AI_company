"""
Shared helpers for the studio tools (products, images, videos, campaigns): finding a
project's product visuals and the company brand. Code picks which files to reuse;
the model never names files.
"""

from PIL import Image

MAX_VISUAL_SIDE = 1800


def brand_name(company) -> str:
    return (getattr(getattr(company, "config", None), "brand_name", "") or "") if company else ""


def latest_product(company, project_id: str | None) -> dict | None:
    """The newest product built for this project: {group_id, title, listing, ...} or None."""
    if company is None or project_id is None:
        return None
    rows = company.assets.list(project_id=project_id, kind="listing", limit=1)
    if not rows:
        rows = company.assets.list(project_id=project_id, kind="product_pdf", limit=1)
    if not rows:
        return None
    row = rows[0]
    return {"group_id": row["group_id"], "title": row["meta"].get("product_title", row["title"]),
            "listing": row["meta"].get("listing"), "theme": row["meta"].get("theme", "modern"),
            "audience": row["meta"].get("audience", "")}


def product_visuals(company, project_id: str | None, limit: int = 8,
                    group_id: str | None = None) -> list[tuple[dict, Image.Image]]:
    """(asset, image) pairs for the newest product: page previews first (cover first),
    then mockups. Empty when the project has no product yet."""
    if company is None or project_id is None:
        return []
    if group_id is None:
        product = latest_product(company, project_id)
        if product is None:
            return []
        group_id = product["group_id"]
    rows = company.assets.list(project_id=project_id, group_id=group_id, limit=200)
    previews = sorted((a for a in rows if a["kind"] == "preview"),
                      key=lambda a: a["meta"].get("page", 99))
    mockups = [a for a in rows if a["kind"] == "mockup"]
    out = []
    for asset in (previews + mockups)[:limit]:
        try:
            with Image.open(company.assets.file_path(asset["id"])) as im:
                im = im.convert("RGB")
                im.thumbnail((MAX_VISUAL_SIDE, MAX_VISUAL_SIDE))
                out.append((asset, im.copy()))
        except (OSError, KeyError):
            continue
    return out
