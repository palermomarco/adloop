"""Image assets in the account's asset library, linked to nothing.

``draft_library_image_assets`` validates images from local files, public
URLs or uploads like ``draft_image_assets``, and the apply creates them as
plain image assets in one GoogleAdsService.Mutate, without a campaign or
account link. The returned resource names are what a Demand Gen ad takes as
``AdImageAsset.asset`` (marketing, square, portrait and logo images).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from adloop.config import AdLoopConfig

# (ratio class, width/height, minimum width, minimum height) for Demand Gen
# image slots; DemandGenMultiAssetAdInfo allows +-1% on each ratio.
_RATIOS = [
    ("landscape", 1.91, 600, 314),
    ("square", 1.0, 300, 300),
    ("portrait", 0.8, 480, 600),
    ("tall_portrait", 9 / 16, 600, 1067),
]
_RATIO_TOLERANCE = 0.01
_LOGO_MIN = 128


def _ratio_class(width: int, height: int) -> tuple[str, str | None]:
    """(ratio class, warning) for an image's dimensions."""
    ratio = width / height if height else 0
    for name, target, min_w, min_h in _RATIOS:
        if abs(ratio - target) / target <= _RATIO_TOLERANCE:
            if width >= min_w and height >= min_h:
                return name, None
            if name == "square" and width >= _LOGO_MIN:
                return name, (
                    f"{width}x{height} is below {min_w}x{min_h} for a square marketing image; "
                    f"usable only as a logo (at least {_LOGO_MIN}x{_LOGO_MIN})"
                )
            return name, f"{width}x{height} is below the {min_w}x{min_h} minimum for a {name} Demand Gen image"
    return "other", (
        f"{width}x{height} fits no Demand Gen image slot "
        "(1.91:1, 1:1, 4:5 or 9:16); crop it before using it in an ad"
    )


def draft_library_image_assets(
    config: AdLoopConfig,
    *,
    customer_id: str = "",
    image_paths: list[str] | None = None,
    image_urls: list[str] | None = None,
    upload_ids: list[str] | None = None,
) -> dict:
    """Draft image assets for the asset library, linked to no campaign.

    Images are validated and pinned by sha256 exactly as in
    ``draft_image_assets``. Each image gets a ratio class (landscape 1.91:1,
    square 1:1, portrait 4:5, tall_portrait 9:16, or other) naming the
    Demand Gen slot it fits; odd ratios and images below Google's minimum
    sizes are warned about, not refused.
    """
    from adloop.ads.write import _describe_image, _validate_image_assets
    from adloop.runtime import deployment_mode
    from adloop.safety.guards import SafetyViolation, check_blocked_operation
    from adloop.safety.preview import ChangePlan, store_plan

    image_paths = list(image_paths or [])
    if image_paths and deployment_mode() == "server":
        return {
            "error": (
                "image_paths can't be used here: local file paths aren't "
                "available on a hosted server; pass image_urls (public "
                "https/http links to PNG, JPEG or GIF images) instead."
            )
        }

    try:
        check_blocked_operation("create_library_image_assets", config.safety)
    except SafetyViolation as e:
        return {"error": str(e)}

    images, errors = _validate_image_assets(image_paths, list(image_urls or []), list(upload_ids or []))
    if errors:
        return {"error": "Validation failed", "details": errors}

    warnings = []
    for index, image in enumerate(images, start=1):
        image["ratio"], warning = _ratio_class(int(image["width"]), int(image["height"]))
        if warning:
            warnings.append(f"Image {index}: {warning}")

    plan = ChangePlan(
        operation="create_library_image_assets",
        entity_type="asset",
        customer_id=customer_id,
        changes={
            "images": images,
            "summary": [f"{_describe_image(image)} ({image['ratio']})" for image in images],
        },
    )
    store_plan(plan)
    preview = plan.to_preview()
    preview["warnings"] = warnings
    return preview


def apply_create_library_image_assets(client: object, cid: str, changes: dict) -> dict:
    """Create the plan's images as library assets in one Mutate.

    Returns their resource names and ids for a Demand Gen ad draft. Google
    reuses an existing image asset with the same content (its name is then
    dropped, per the Google Ads API "Working with assets" guide), so the name
    returned can be an asset that was already in the account. URL images and
    uploads must still match the sha256 approved in the preview.
    """
    from adloop.ads.write import populate_image_asset

    operations = []
    for payload in changes["images"]:
        op = client.get_type("MutateOperation")
        populate_image_asset(client, op.asset_operation.create, payload)
        operations.append(op)
    response = client.get_service("GoogleAdsService").mutate(customer_id=cid, mutate_operations=operations)

    assets = []
    for payload, resp in zip(changes["images"], response.mutate_operation_responses):
        resource_name = resp.asset_result.resource_name
        assets.append({
            "resource_name": resource_name,
            "asset_id": resource_name.rsplit("/", 1)[-1],
            "name": payload.get("name", ""),
            "width": payload["width"],
            "height": payload["height"],
            "ratio": payload.get("ratio", ""),
        })
    return {"assets": assets}
