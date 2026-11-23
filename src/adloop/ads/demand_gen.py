"""Demand Gen campaigns and image ads: draft → preview → apply.

The campaign is one atomic GoogleAdsService.Mutate, following Google's
"Create a Demand Gen campaign" guide:

1. CampaignBudget (not shared)
2. Campaign: DEMAND_GEN channel, no sub type, created PAUSED
3. AdGroup without a type. ``audience_setting.use_audience_grouped`` is set
   because it is immutable and an Audience can only be attached with it
4. Location and language criteria on the AD GROUP: Demand Gen targets
   there, not on the campaign

The ad is a DemandGenMultiAssetAdInfo built from image assets that are
already in the asset library (see image_library.py).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from adloop.config import AdLoopConfig

# Maximize clicks is TARGET_SPEND in the API, as in draft_campaign.
DEMAND_GEN_BIDDING_STRATEGIES = {"TARGET_SPEND", "MAXIMIZE_CONVERSIONS", "TARGET_CPA"}
DEMAND_GEN_CHANNELS = (
    "youtube_in_stream", "youtube_in_feed", "youtube_shorts", "discover", "gmail", "display", "maps",
)

_BUDGET, _CAMPAIGN, _AD_GROUP = "-1", "-2", "-3"

# Parameter -> AdGroupAd.ad_group_ad_asset_automation_settings type. Google opts
# multi-asset ads into both by default and builds extra ads from their images
# and texts. Demand Gen refuses these on the campaign (VALUE_MUST_BE_UNSET).
GENERATED_MEDIA = {
    "generated_videos": "GENERATE_VIDEOS_FROM_OTHER_ASSETS",
    "animated_images": "GENERATE_ANIMATED_IMAGES_FROM_OTHER_ASSETS",
}

# Google's Demand Gen image ad limits (Help Center "Demand Gen asset specs").
_MAX_HEADLINE, _MAX_DESCRIPTION, _MAX_BUSINESS_NAME = 40, 90, 25
_MAX_TEXTS, _MAX_LOGOS, _MAX_MARKETING_IMAGES = 5, 5, 20
# name -> (ad field, width / height); Google accepts ±1%.
_IMAGE_SLOTS = {
    "marketing_image_asset_ids": ("marketing_images", 1.91),
    "square_image_asset_ids": ("square_marketing_images", 1.0),
    "portrait_image_asset_ids": ("portrait_marketing_images", 0.8),
    "logo_asset_ids": ("logo_images", 1.0),
}


def _ids(values: list[str] | None) -> list[str]:
    return [str(v).strip() for v in (values or []) if str(v).strip()]


def draft_demand_gen_campaign(
    config: AdLoopConfig,
    *,
    customer_id: str = "",
    campaign_name: str = "",
    daily_budget: float = 0,
    bidding_strategy: str = "MAXIMIZE_CONVERSIONS",
    target_cpa: float = 0,
    max_cpc: float = 0,
    start_date: str = "",
    end_date: str = "",
    ad_group_name: str = "",
    geo_target_ids: list[str] | None = None,
    language_ids: list[str] | None = None,
    channels: list[str] | None = None,
    optimized_targeting: bool = False,
) -> dict:
    """Draft a Demand Gen campaign with one ad group (created PAUSED) — returns a preview."""
    from adloop.ads.assets import _show_date_errors
    from adloop.safety.guards import SafetyViolation, check_blocked_operation, check_budget_cap
    from adloop.safety.preview import ChangePlan, store_plan

    try:
        check_blocked_operation("create_demand_gen_campaign", config.safety)
    except SafetyViolation as e:
        return {"error": str(e)}

    errors: list[str] = []
    warnings: list[str] = []
    campaign_name = (campaign_name or "").strip()
    if not campaign_name:
        errors.append("campaign_name is required")
    if not daily_budget or daily_budget <= 0:
        errors.append("daily_budget must be greater than 0")
    bs = (bidding_strategy or "").strip().upper()
    if bs not in DEMAND_GEN_BIDDING_STRATEGIES:
        errors.append(
            f"bidding_strategy must be one of {sorted(DEMAND_GEN_BIDDING_STRATEGIES)} for Demand Gen "
            "(TARGET_SPEND is Maximize clicks)"
        )
    if bs == "TARGET_CPA" and not target_cpa:
        errors.append("target_cpa is required for TARGET_CPA")
    if max_cpc and bs != "TARGET_SPEND":
        errors.append("max_cpc only applies to TARGET_SPEND (Maximize clicks)")
    geo_target_ids, language_ids = _ids(geo_target_ids), _ids(language_ids)
    for label, ids in (("geo_target_ids", geo_target_ids), ("language_ids", language_ids)):
        if not ids:
            errors.append(f"{label} is required: Demand Gen ad groups without it serve everywhere")
        elif [i for i in ids if not i.isdigit()]:
            errors.append(f"{label} must be numeric constant ids, got {[i for i in ids if not i.isdigit()]}")
    start_date, end_date = (start_date or "").strip(), (end_date or "").strip()
    errors.extend(_show_date_errors("", start_date, end_date))
    channels = [str(c).strip().lower() for c in (channels or []) if str(c).strip()]
    unknown = [c for c in channels if c not in DEMAND_GEN_CHANNELS]
    if unknown:
        errors.append(f"channels must be among {list(DEMAND_GEN_CHANNELS)}, got {unknown}")
    if errors:
        return {"error": "Validation failed", "details": errors}

    try:
        check_budget_cap(daily_budget, config.safety)
    except SafetyViolation as e:
        return {"error": str(e)}

    if not channels:
        warnings.append(
            "No channels given: the ad group serves on every Demand Gen channel, including the "
            "Display Network. Pass channels to limit it."
        )
    if optimized_targeting:
        warnings.append("Optimized targeting is ON: Google also reaches people outside the chosen audience.")
    if bs != "TARGET_SPEND":
        warnings.append("Conversion bidding needs working conversion tracking; check it before enabling.")
    if target_cpa and daily_budget < 15 * target_cpa:
        warnings.append(
            f"Google recommends a daily budget of at least 15x the target CPA ({15 * target_cpa:g}); "
            f"{daily_budget:g} may keep the campaign in learning."
        )
    warnings.append(
        "The campaign is created PAUSED and cannot serve until the ad group has an audience "
        "(draft_audience) and an ad (draft_demand_gen_ad)."
    )

    plan = ChangePlan(
        operation="create_demand_gen_campaign",
        entity_type="campaign",
        customer_id=customer_id,
        changes={
            "campaign_name": campaign_name,
            "daily_budget": daily_budget,
            "bidding_strategy": bs,
            "target_cpa": target_cpa or None,
            "max_cpc": max_cpc or None,
            "start_date": start_date or None,
            "end_date": end_date or None,
            "ad_group_name": (ad_group_name or "").strip() or campaign_name,
            "geo_target_ids": geo_target_ids,
            "language_ids": language_ids,
            "channels": channels or "ALL_CHANNELS",
            "optimized_targeting": bool(optimized_targeting),
            "status_on_create": "PAUSED",
        },
    )
    store_plan(plan)
    preview = plan.to_preview()
    preview["warnings"] = warnings
    return preview


def build_demand_gen_campaign_operations(client: object, cid: str, changes: dict) -> list:
    """The atomic mutate for a Demand Gen campaign (see module doc)."""
    service = client.get_service("GoogleAdsService")
    budget_path = service.campaign_budget_path(cid, _BUDGET)
    campaign_path = service.campaign_path(cid, _CAMPAIGN)
    ad_group_path = service.ad_group_path(cid, _AD_GROUP)
    operations = []

    op = client.get_type("MutateOperation")
    budget = op.campaign_budget_operation.create
    budget.resource_name = budget_path
    budget.name = f"Budget - {changes['campaign_name']}"
    budget.amount_micros = int(changes["daily_budget"] * 1_000_000)
    budget.delivery_method = client.enums.BudgetDeliveryMethodEnum.STANDARD
    budget.explicitly_shared = False
    operations.append(op)

    op = client.get_type("MutateOperation")
    campaign = op.campaign_operation.create
    campaign.resource_name = campaign_path
    campaign.name = changes["campaign_name"]
    campaign.campaign_budget = budget_path
    campaign.status = client.enums.CampaignStatusEnum.PAUSED
    campaign.advertising_channel_type = client.enums.AdvertisingChannelTypeEnum.DEMAND_GEN
    bs = changes["bidding_strategy"]
    if bs == "TARGET_SPEND":
        campaign.target_spend.target_spend_micros = 0
        if changes.get("max_cpc"):
            campaign.target_spend.cpc_bid_ceiling_micros = int(changes["max_cpc"] * 1_000_000)
    elif bs == "MAXIMIZE_CONVERSIONS":
        campaign.maximize_conversions.target_cpa_micros = 0
    elif bs == "TARGET_CPA":
        campaign.target_cpa.target_cpa_micros = int(changes["target_cpa"] * 1_000_000)
    # Account time zone; 00:00:00 / 23:59:59 are Google's whole-day values.
    if changes.get("start_date"):
        campaign.start_date_time = f"{changes['start_date']} 00:00:00"
    if changes.get("end_date"):
        campaign.end_date_time = f"{changes['end_date']} 23:59:59"
    campaign.contains_eu_political_advertising = (
        client.enums.EuPoliticalAdvertisingStatusEnum.DOES_NOT_CONTAIN_EU_POLITICAL_ADVERTISING
    )
    operations.append(op)

    op = client.get_type("MutateOperation")
    ad_group = op.ad_group_operation.create
    ad_group.resource_name = ad_group_path
    ad_group.name = changes["ad_group_name"]
    ad_group.campaign = campaign_path
    ad_group.status = client.enums.AdGroupStatusEnum.ENABLED
    ad_group.audience_setting.use_audience_grouped = True
    ad_group.optimized_targeting_enabled = bool(changes.get("optimized_targeting"))
    channels = changes.get("channels")
    if isinstance(channels, list):
        selected = ad_group.demand_gen_ad_group_settings.channel_controls.selected_channels
        # Google wants every flag stated, so the unchosen ones are sent as False.
        for channel in DEMAND_GEN_CHANNELS:
            setattr(selected, channel, channel in channels)
    operations.append(op)

    for geo_id in changes["geo_target_ids"]:
        op = client.get_type("MutateOperation")
        criterion = op.ad_group_criterion_operation.create
        criterion.ad_group = ad_group_path
        criterion.location.geo_target_constant = f"geoTargetConstants/{geo_id}"
        operations.append(op)
    for language_id in changes["language_ids"]:
        op = client.get_type("MutateOperation")
        criterion = op.ad_group_criterion_operation.create
        criterion.ad_group = ad_group_path
        criterion.language.language_constant = f"languageConstants/{language_id}"
        operations.append(op)

    return operations


def apply_create_demand_gen_campaign(client: object, cid: str, changes: dict) -> dict:
    from adloop.ads.write import _extract_resource_name

    service = client.get_service("GoogleAdsService")
    response = service.mutate(
        customer_id=cid, mutate_operations=build_demand_gen_campaign_operations(client, cid, changes)
    )
    names = [_extract_resource_name(r) for r in response.mutate_operation_responses]
    names += [None] * (3 - len(names))
    result = {
        "campaign_budget": names[0],
        "campaign": names[1],
        "ad_group": names[2],
        "optimized_targeting": bool(changes.get("optimized_targeting")),
        "status": "PAUSED",
        "next_steps": (
            "Attach an audience with draft_audience(ad_group_id=...), upload images with "
            "draft_library_image_assets, add the ad with draft_demand_gen_ad, then enable the "
            "campaign with enable_entity(entity_type='campaign')."
        ),
    }
    if not changes.get("optimized_targeting") and names[2]:
        # The field has no presence, so False cannot be sent on create, and
        # Google refuses an update of a temporary id in the same request. The
        # campaign already exists (PAUSED), so a failure here is reported,
        # not raised: raising would invite a second, duplicate apply.
        op = client.get_type("MutateOperation")
        op.ad_group_operation.update.resource_name = names[2]
        op.ad_group_operation.update.optimized_targeting_enabled = False
        op.ad_group_operation.update_mask.paths.append("optimized_targeting_enabled")
        try:
            service.mutate(customer_id=cid, mutate_operations=[op])
        except Exception as e:  # noqa: BLE001
            result["optimized_targeting"] = f"NOT VERIFIED OFF, check it in Google Ads before enabling: {e}"
    return result


# ---------------------------------------------------------------------------
# Multi-asset (image) ad
# ---------------------------------------------------------------------------


def _library_images(config: AdLoopConfig, customer_id: str, asset_ids: list[str]) -> dict[str, dict]:
    """Image assets of the account by id: ``{id: {"width", "height"}}``."""
    from adloop.ads.gaql import execute_query

    rows = execute_query(
        config,
        customer_id,
        "SELECT asset.id, asset.image_asset.full_size.width_pixels, "
        "asset.image_asset.full_size.height_pixels FROM asset "
        f"WHERE asset.type = 'IMAGE' AND asset.id IN ({', '.join(sorted(set(asset_ids)))})",
    )
    return {
        str(row.get("asset.id")): {
            "width": int(row.get("asset.image_asset.full_size.width_pixels") or 0),
            "height": int(row.get("asset.image_asset.full_size.height_pixels") or 0),
        }
        for row in rows
    }


def draft_demand_gen_ad(
    config: AdLoopConfig,
    *,
    customer_id: str = "",
    ad_group_id: str = "",
    final_url: str = "",
    headlines: list[str] | None = None,
    descriptions: list[str] | None = None,
    business_name: str = "",
    marketing_image_asset_ids: list[str] | None = None,
    square_image_asset_ids: list[str] | None = None,
    portrait_image_asset_ids: list[str] | None = None,
    logo_asset_ids: list[str] | None = None,
    call_to_action: str = "",
    ad_name: str = "",
    generated_videos: bool = False,
    animated_images: bool = False,
) -> dict:
    """Draft a Demand Gen multi-asset image ad (created PAUSED) — returns a preview."""
    from adloop.ads.write import _counted_length, _validate_urls
    from adloop.safety.guards import SafetyViolation, check_blocked_operation
    from adloop.safety.preview import ChangePlan, store_plan

    try:
        check_blocked_operation("create_demand_gen_ad", config.safety)
    except SafetyViolation as e:
        return {"error": str(e)}

    errors: list[str] = []
    warnings: list[str] = []
    ad_group_id = str(ad_group_id or "").strip()
    if not ad_group_id.isdigit():
        errors.append("ad_group_id is required: the numeric id of a Demand Gen ad group")
    final_url = (final_url or "").strip()
    if not final_url:
        errors.append("final_url is required")
    headlines = [str(h).strip() for h in (headlines or []) if str(h).strip()]
    descriptions = [str(d).strip() for d in (descriptions or []) if str(d).strip()]
    for label, texts, cap in (("headline", headlines, _MAX_HEADLINE), ("description", descriptions, _MAX_DESCRIPTION)):
        if not 1 <= len(texts) <= _MAX_TEXTS:
            errors.append(f"{label}s: 1 to {_MAX_TEXTS} required, got {len(texts)}")
        for text in texts:
            if _counted_length(text) > cap:
                errors.append(f"{label} '{text}' has {_counted_length(text)} characters (max {cap})")
        if len(set(texts)) != len(texts):
            errors.append(f"{label}s must be unique")
    business_name = (business_name or "").strip()
    if not business_name:
        errors.append("business_name is required")
    elif _counted_length(business_name) > _MAX_BUSINESS_NAME:
        errors.append(
            f"business_name has {_counted_length(business_name)} characters (max {_MAX_BUSINESS_NAME})"
        )

    images = {
        "marketing_image_asset_ids": _ids(marketing_image_asset_ids),
        "square_image_asset_ids": _ids(square_image_asset_ids),
        "portrait_image_asset_ids": _ids(portrait_image_asset_ids),
        "logo_asset_ids": _ids(logo_asset_ids),
    }
    all_ids = [i for ids in images.values() for i in ids]
    if [i for i in all_ids if not i.isdigit()]:
        errors.append(
            "image and logo ids must be numeric asset ids (from draft_library_image_assets), "
            f"got {[i for i in all_ids if not i.isdigit()]}"
        )
    if not images["marketing_image_asset_ids"] and not images["square_image_asset_ids"]:
        errors.append("at least one landscape (1.91:1) or square (1:1) image is required")
    if not 1 <= len(images["logo_asset_ids"]) <= _MAX_LOGOS:
        errors.append(f"logo_asset_ids: 1 to {_MAX_LOGOS} required, got {len(images['logo_asset_ids'])}")
    marketing = len(all_ids) - len(images["logo_asset_ids"])
    if marketing > _MAX_MARKETING_IMAGES:
        errors.append(f"at most {_MAX_MARKETING_IMAGES} marketing images in total, got {marketing}")
    if errors:
        return {"error": "Validation failed", "details": errors}

    known = _library_images(config, customer_id, all_ids)
    for name, ids in images.items():
        ratio = _IMAGE_SLOTS[name][1]
        for asset_id in ids:
            image = known.get(asset_id)
            if image is None:
                errors.append(f"{name}: asset {asset_id} is not an image asset of this account")
            elif not image["height"] or abs(image["width"] / image["height"] / ratio - 1) > 0.01:
                errors.append(
                    f"{name}: asset {asset_id} is {image['width']}x{image['height']}, "
                    f"this slot needs a {ratio:g}:1 image"
                )
    url_check, url_warnings = _validate_urls([final_url])
    if url_check.get(final_url):
        errors.append(f"final_url: {url_check[final_url]}")
    warnings.extend(url_warnings.values())
    if errors:
        return {"error": "Validation failed", "details": errors}

    warnings.append("The ad is created PAUSED; enable it with enable_entity(entity_type='ad') after review.")
    plan = ChangePlan(
        operation="create_demand_gen_ad",
        entity_type="ad",
        entity_id=ad_group_id,
        customer_id=customer_id,
        changes={
            "ad_group_id": ad_group_id,
            "ad_name": (ad_name or "").strip() or None,
            "final_url": final_url,
            "headlines": headlines,
            "descriptions": descriptions,
            "business_name": business_name,
            **images,
            "call_to_action": (call_to_action or "").strip() or None,
            "generated_videos": bool(generated_videos),
            "animated_images": bool(animated_images),
            "status_on_create": "PAUSED",
        },
    )
    store_plan(plan)
    preview = plan.to_preview()
    preview["warnings"] = warnings
    return preview


def apply_create_demand_gen_ad(client: object, cid: str, changes: dict) -> dict:
    from adloop.ads.write import _extract_resource_name

    service = client.get_service("GoogleAdsService")
    op = client.get_type("MutateOperation")
    ad_group_ad = op.ad_group_ad_operation.create
    ad_group_ad.ad_group = service.ad_group_path(cid, changes["ad_group_id"])
    ad_group_ad.status = client.enums.AdGroupAdStatusEnum.PAUSED
    for name, asset_type in GENERATED_MEDIA.items():
        # Plans drafted before these options existed keep Google's default.
        if name in changes:
            ad_group_ad.ad_group_ad_asset_automation_settings.append({
                "asset_automation_type": asset_type,
                "asset_automation_status": "OPTED_IN" if changes[name] else "OPTED_OUT",
            })
    ad = ad_group_ad.ad
    if changes.get("ad_name"):
        ad.name = changes["ad_name"]
    ad.final_urls.append(changes["final_url"])
    info = ad.demand_gen_multi_asset_ad
    for field in ("headlines", "descriptions"):
        for text in changes[field]:
            asset = client.get_type("AdTextAsset")
            asset.text = text
            getattr(info, field).append(asset)
    info.business_name = changes["business_name"]
    if changes.get("call_to_action"):
        info.call_to_action_text = changes["call_to_action"]
    for name, (field, _ratio) in _IMAGE_SLOTS.items():
        for asset_id in changes.get(name) or []:
            image = client.get_type("AdImageAsset")
            image.asset = service.asset_path(cid, asset_id)
            getattr(info, field).append(image)

    response = service.mutate(customer_id=cid, mutate_operations=[op])
    names = [_extract_resource_name(r) for r in response.mutate_operation_responses]
    return {"ad": names[0] if names else None, "status": "PAUSED"}
