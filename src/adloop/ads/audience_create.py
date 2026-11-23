"""Create Demand Gen audiences: custom segments, lookalike lists, audiences.

Three draft tools, each one atomic request:

- ``draft_custom_segment`` creates a CustomAudience of URL and KEYWORD members:
  people who browse sites like these URLs, or search for / are interested in
  these terms. Members are OR-ed (anyone matching one member is reached).
  CustomAudience is not part of GoogleAdsService.Mutate in v25, so the apply
  goes through CustomAudienceService.MutateCustomAudiences.
- ``draft_lookalike_list`` creates a UserList with ``lookalike_user_list``:
  people similar to the seed lists. Lookalikes serve only in Demand Gen; the
  seeds together need at least 100 active matched people, below that Google
  marks the segment Ineligible without an error. The list is immutable: other
  seeds, level or countries mean a new list.
- ``draft_audience`` creates an Audience (scope CUSTOMER) from custom segments
  and user lists (lookalikes included). All segments go in ONE
  AudienceSegmentDimension, whose segments Google OR-s ("users are included if
  they belong to at least one segment"). With ``ad_group_id`` the same
  GoogleAdsService.Mutate also attaches the audience to that Demand Gen ad
  group (AdGroupCriterion.audience), referencing the new audience by its
  temporary resource name ``customers/{cid}/audiences/-1``. The ad group must
  have ``audience_setting.use_audience_grouped`` on, which Google only allows
  at ad group creation.

Names must be unique per account for all three; the drafts check the names
they can read and Google's dry run checks the rest.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

if TYPE_CHECKING:
    from adloop.config import AdLoopConfig

# INTEREST and PURCHASE_INTENT exist in the enum but Google refuses them for
# new custom segments ("use AUTO instead").
_SEGMENT_TYPES = {"AUTO", "SEARCH"}
_LEGACY_SEGMENT_TYPES = {"INTEREST", "PURCHASE_INTENT"}
_EXPANSION_LEVELS = {"NARROW", "BALANCED", "BROAD"}
_KEYWORD_MAX_CHARS = 80
_KEYWORD_MAX_WORDS = 10
_URL_MAX_CHARS = 2048
_NAME_MAX_CHARS = 255
_LOOKALIKE_MIN_SEED_SIZE = 100


def _ids(values: list | None) -> list[str]:
    return list(dict.fromkeys(str(v).strip() for v in values or [] if str(v).strip()))


def _check_name(name: str, taken: set[str], what: str, errors: list[str]) -> None:
    if not name:
        errors.append("name is required")
    elif len(name) > _NAME_MAX_CHARS:
        errors.append(f"name must be at most {_NAME_MAX_CHARS} characters")
    elif name.casefold() in taken:
        errors.append(f"{what} named '{name}' already exists in this account; names must be unique")


def _normalize_url(raw: str) -> str | None:
    """The URL with a scheme (Google wants it "protocol-included"), or None."""
    url = raw.strip()
    if "://" not in url:
        url = "https://" + url
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or "." not in parts.netloc or " " in url or len(url) > _URL_MAX_CHARS:
        return None
    return url


def draft_custom_segment(
    config: AdLoopConfig,
    *,
    customer_id: str = "",
    name: str = "",
    urls: list[str] | None = None,
    keywords: list[str] | None = None,
    segment_type: str = "AUTO",
    description: str = "",
) -> dict:
    """Draft a custom segment (CustomAudience) of URL and keyword members — returns a preview."""
    from adloop.ads.audiences import get_audience_lists
    from adloop.safety.guards import SafetyViolation, check_blocked_operation
    from adloop.safety.preview import ChangePlan, store_plan

    try:
        check_blocked_operation("create_custom_segment", config.safety)
    except SafetyViolation as e:
        return {"error": str(e)}

    errors: list[str] = []
    warnings: list[str] = []
    name = (name or "").strip()
    kind = (segment_type or "AUTO").strip().upper()
    if kind in _LEGACY_SEGMENT_TYPES:
        errors.append(f"segment_type {kind} cannot be used for new custom segments; use AUTO (or SEARCH)")
    elif kind not in _SEGMENT_TYPES:
        errors.append(f"segment_type must be one of {sorted(_SEGMENT_TYPES)}")

    clean_urls: list[str] = []
    for raw in dict.fromkeys(str(u).strip() for u in urls or [] if str(u).strip()):
        url = _normalize_url(raw)
        if url is None:
            errors.append(f"'{raw}' is not a valid http(s) URL (at most {_URL_MAX_CHARS} characters)")
        elif url not in clean_urls:
            clean_urls.append(url)
    clean_keywords: list[str] = []
    seen: set[str] = set()
    for raw in keywords or []:
        keyword = " ".join(str(raw).split())
        if not keyword or keyword.casefold() in seen:
            continue
        seen.add(keyword.casefold())
        if len(keyword) > _KEYWORD_MAX_CHARS or len(keyword.split()) > _KEYWORD_MAX_WORDS:
            errors.append(f"keyword '{keyword}' is too long: at most {_KEYWORD_MAX_WORDS} words and {_KEYWORD_MAX_CHARS} characters")
        else:
            clean_keywords.append(keyword)
    if not clean_urls and not clean_keywords and not errors:
        errors.append("Pass at least one of urls or keywords")
    if not name:
        errors.append("name is required")
    if errors:
        return {"error": "Validation failed", "details": errors}

    lists = get_audience_lists(config, customer_id=customer_id)
    _check_name(name, {c["name"].casefold() for c in lists["custom_segments"] if c.get("name")}, "A custom segment", errors)
    if errors:
        return {"error": "Validation failed", "details": errors}

    if kind == "SEARCH":
        warnings.append("SEARCH reaches people by what they searched on Google; AUTO lets Google also use interests and browsing.")
    warnings.append(
        "Custom segments reach people matching ANY member. They can target Demand Gen (inside an audience, "
        "see draft_audience), Display and Video, never Search campaigns."
    )

    plan = ChangePlan(
        operation="create_custom_segment",
        entity_type="custom_audience",
        customer_id=customer_id,
        changes={
            "name": name,
            "description": (description or "").strip() or None,
            "segment_type": kind,
            "urls": clean_urls,
            "keywords": clean_keywords,
        },
    )
    store_plan(plan)
    preview = plan.to_preview()
    preview["warnings"] = warnings
    return preview


def apply_create_custom_segment(client: object, cid: str, changes: dict) -> dict:
    """One CustomAudienceService call (CustomAudience is not in MutateOperation)."""
    service = client.get_service("CustomAudienceService")
    operation = client.get_type("CustomAudienceOperation")
    audience = operation.create
    audience.name = changes["name"]
    if changes.get("description"):
        audience.description = changes["description"]
    audience.type_ = getattr(client.enums.CustomAudienceTypeEnum, changes["segment_type"])
    member_type = client.enums.CustomAudienceMemberTypeEnum
    for url in changes["urls"]:
        member = client.get_type("CustomAudienceMember")
        member.member_type = member_type.URL
        member.url = url
        audience.members.append(member)
    for keyword in changes["keywords"]:
        member = client.get_type("CustomAudienceMember")
        member.member_type = member_type.KEYWORD
        member.keyword = keyword
        audience.members.append(member)
    response = service.mutate_custom_audiences(customer_id=cid, operations=[operation])
    resource = response.results[0].resource_name
    return {
        "custom_audience": resource,
        "custom_audience_id": resource.rsplit("/", 1)[-1],
        "members": len(changes["urls"]) + len(changes["keywords"]),
        "next_steps": "Combine it into an audience with draft_audience(custom_audience_ids=[...]) to use it in Demand Gen.",
    }


def draft_lookalike_list(
    config: AdLoopConfig,
    *,
    customer_id: str = "",
    name: str = "",
    seed_user_list_ids: list[str] | None = None,
    expansion_level: str = "BALANCED",
    country_codes: list[str] | None = None,
    description: str = "",
) -> dict:
    """Draft a lookalike user list from seed lists (Demand Gen only) — returns a preview."""
    from adloop.ads.audiences import get_audience_lists
    from adloop.safety.guards import SafetyViolation, check_blocked_operation
    from adloop.safety.preview import ChangePlan, store_plan

    try:
        check_blocked_operation("create_lookalike_list", config.safety)
    except SafetyViolation as e:
        return {"error": str(e)}

    errors: list[str] = []
    warnings: list[str] = []
    name = (name or "").strip()
    if not name:
        errors.append("name is required")
    seeds = _ids(seed_user_list_ids)
    if not seeds:
        errors.append("seed_user_list_ids is required: the user lists the lookalike is built from (see get_audience_lists)")
    bad = [s for s in seeds if not s.isdigit()]
    if bad:
        errors.append(f"seed_user_list_ids must be numeric: {', '.join(bad)}")
    level = (expansion_level or "BALANCED").strip().upper()
    if level not in _EXPANSION_LEVELS:
        errors.append(f"expansion_level must be one of {sorted(_EXPANSION_LEVELS)}")
    countries = list(dict.fromkeys(str(c).strip().upper() for c in country_codes or [] if str(c).strip()))
    bad = [c for c in countries if not re.fullmatch(r"[A-Z]{2}", c)]
    if bad:
        errors.append(f"country_codes must be ISO 3166-1 alpha-2 codes such as IT or DE, got {bad}")
    if errors:
        return {"error": "Validation failed", "details": errors}

    lists = get_audience_lists(config, customer_id=customer_id)
    known = {u["user_list_id"]: u for u in lists["user_lists"]}
    _check_name(name, {u["name"].casefold() for u in lists["user_lists"] if u.get("name")}, "A user list", errors)
    missing = [s for s in seeds if s not in known]
    if missing:
        errors.append(f"user lists {', '.join(missing)} are not lists of this account (see get_audience_lists)")
    if errors:
        return {"error": "Validation failed", "details": errors}

    seed_info = [known[s] for s in seeds]
    total = sum(u.get("size_for_display") or 0 for u in seed_info)
    if total < _LOOKALIKE_MIN_SEED_SIZE:
        warnings.append(
            f"The seed lists hold about {total:,} people; a lookalike needs at least {_LOOKALIKE_MIN_SEED_SIZE:,} "
            "active matched people across its seeds. Below that it stays Ineligible and serves nothing (no error)."
        )
    closed = [u["name"] for u in seed_info if u.get("membership_status") == "CLOSED"]
    if closed:
        warnings.append("Closed seed lists no longer add members, so the seed shrinks over time: " + ", ".join(closed))
    if not countries:
        warnings.append("No country_codes: Google decides where to find similar people. Pass the countries the campaign targets.")
    warnings.append(
        "Lookalike lists only work in Demand Gen campaigns (elsewhere they show as Eligible - Limited). Google builds "
        "it in 1-2 days, so create it a few days before launch. Seeds, level and countries cannot be changed later."
    )

    plan = ChangePlan(
        operation="create_lookalike_list",
        entity_type="user_list",
        customer_id=customer_id,
        changes={
            "name": name,
            "description": (description or "").strip() or None,
            "seed_user_list_ids": seeds,
            "seed_lists": [{"user_list_id": u["user_list_id"], "name": u["name"]} for u in seed_info],
            "expansion_level": level,
            "country_codes": countries,
        },
    )
    store_plan(plan)
    preview = plan.to_preview()
    preview["warnings"] = warnings
    return preview


def apply_create_lookalike_list(client: object, cid: str, changes: dict) -> dict:
    """One UserListService call creating the lookalike list."""
    service = client.get_service("UserListService")
    operation = client.get_type("UserListOperation")
    user_list = operation.create
    user_list.name = changes["name"]
    if changes.get("description"):
        user_list.description = changes["description"]
    lookalike = user_list.lookalike_user_list
    lookalike.seed_user_list_ids.extend(int(s) for s in changes["seed_user_list_ids"])
    lookalike.expansion_level = getattr(client.enums.LookalikeExpansionLevelEnum, changes["expansion_level"])
    lookalike.country_codes.extend(changes["country_codes"])
    response = service.mutate_user_lists(customer_id=cid, operations=[operation])
    resource = response.results[0].resource_name
    return {
        "user_list": resource,
        "user_list_id": resource.rsplit("/", 1)[-1],
        "next_steps": "Use it in Demand Gen through an audience: draft_audience(user_list_ids=[...]).",
    }


def draft_audience(
    config: AdLoopConfig,
    *,
    customer_id: str = "",
    name: str = "",
    custom_audience_ids: list[str] | None = None,
    user_list_ids: list[str] | None = None,
    description: str = "",
    ad_group_id: str = "",
) -> dict:
    """Draft an audience of custom segments and user lists, optionally attached to a Demand Gen ad group."""
    from adloop.ads.audiences import get_audience_lists
    from adloop.ads.gaql import execute_query
    from adloop.safety.guards import SafetyViolation, check_blocked_operation
    from adloop.safety.preview import ChangePlan, store_plan

    try:
        check_blocked_operation("create_audience", config.safety)
    except SafetyViolation as e:
        return {"error": str(e)}

    errors: list[str] = []
    warnings: list[str] = []
    name = (name or "").strip()
    if not name:
        errors.append("name is required")
    custom_ids, list_ids = _ids(custom_audience_ids), _ids(user_list_ids)
    if not custom_ids and not list_ids:
        errors.append("Pass at least one of custom_audience_ids or user_list_ids")
    bad = [i for i in custom_ids + list_ids if not i.isdigit()]
    ad_group_id = str(ad_group_id or "").strip()
    if ad_group_id and not ad_group_id.isdigit():
        bad.append(ad_group_id)
    if bad:
        errors.append(f"ids must be numeric Google Ads ids: {', '.join(bad)}")
    if errors:
        return {"error": "Validation failed", "details": errors}

    lists = get_audience_lists(config, customer_id=customer_id)
    customs = {c["custom_audience_id"]: c for c in lists["custom_segments"]}
    user_lists = {u["user_list_id"]: u for u in lists["user_lists"]}
    taken = {
        str(r.get("audience.name") or "").casefold()
        for r in execute_query(config, customer_id, "SELECT audience.id, audience.name FROM audience WHERE audience.status = 'ENABLED'")
    }
    _check_name(name, taken, "An audience", errors)
    missing = [i for i in custom_ids if i not in customs]
    if missing:
        errors.append(f"custom segments {', '.join(missing)} are not custom segments of this account (see get_audience_lists)")
    missing = [i for i in list_ids if i not in user_lists]
    if missing:
        errors.append(f"user lists {', '.join(missing)} are not lists of this account (see get_audience_lists)")

    ad_group = None
    if ad_group_id:
        rows = execute_query(
            config,
            customer_id,
            "SELECT ad_group.id, ad_group.name, ad_group.audience_setting.use_audience_grouped, "
            "campaign.id, campaign.name, campaign.advertising_channel_type "
            f"FROM ad_group WHERE ad_group.id = {ad_group_id}",
        )
        if not rows:
            errors.append(f"ad group {ad_group_id} was not found in this account")
        else:
            row = rows[0]
            ad_group = {"ad_group_id": ad_group_id, "name": row.get("ad_group.name"),
                        "campaign_id": str(row.get("campaign.id")), "campaign_name": row.get("campaign.name")}
            if row.get("campaign.advertising_channel_type") != "DEMAND_GEN":
                errors.append(
                    f"ad group '{ad_group['name']}' is in a {row.get('campaign.advertising_channel_type')} campaign; "
                    "audiences attach this way only to Demand Gen ad groups (Display/Video/Search: draft_audience_attachment)"
                )
            elif not row.get("ad_group.audience_setting.use_audience_grouped"):
                errors.append(
                    f"ad group '{ad_group['name']}' was created without audience_setting.use_audience_grouped, which "
                    "Google only allows at creation; it cannot take an audience. Create the audience without "
                    "ad_group_id, or use a new ad group created with that setting."
                )
            elif execute_query(
                config,
                customer_id,
                "SELECT ad_group_criterion.criterion_id FROM ad_group_criterion WHERE ad_group_criterion.type = "
                f"'AUDIENCE' AND ad_group_criterion.status != 'REMOVED' AND ad_group.id = {ad_group_id}",
            ):
                # Google: ONE_AUDIENCE_ALLOWED_PER_AD_GROUP.
                errors.append(
                    f"ad group '{ad_group['name']}' already has an audience and takes only one. Remove it first "
                    "(get_audience_targeting gives the remove_id for remove_entity), or create the audience "
                    "without ad_group_id."
                )
    if errors:
        return {"error": "Validation failed", "details": errors}

    segments = (
        [{"kind": "custom_audience", "id": i, "name": customs[i]["name"]} for i in custom_ids]
        + [{"kind": "user_list", "id": i, "name": user_lists[i]["name"]} for i in list_ids]
    )
    closed = [user_lists[i]["name"] for i in list_ids if user_lists[i].get("membership_status") == "CLOSED"]
    if closed:
        warnings.append("Closed lists no longer add members: " + ", ".join(closed))
    warnings.append(
        "The audience reaches people in ANY of its segments (one segment dimension, OR). "
        + ("Attached to the ad group, the ads only reach this audience." if ad_group else
           "Nothing serves differently until it is attached to a Demand Gen ad group.")
    )

    plan = ChangePlan(
        operation="create_audience",
        entity_type="audience",
        entity_id=ad_group_id,
        customer_id=customer_id,
        changes={
            "name": name,
            "description": (description or "").strip() or None,
            "scope": "CUSTOMER",
            "segment_logic": "OR",
            "segments": segments,
            "ad_group": ad_group,
        },
    )
    store_plan(plan)
    preview = plan.to_preview()
    preview["warnings"] = warnings
    return preview


def apply_create_audience(client: object, cid: str, changes: dict) -> dict:
    """One GoogleAdsService.Mutate: the audience, then its ad group criterion (temporary id -1)."""
    service = client.get_service("GoogleAdsService")
    operations = []

    op = client.get_type("MutateOperation")
    audience = op.audience_operation.create
    audience.name = changes["name"]
    if changes.get("description"):
        audience.description = changes["description"]
    audience.scope = client.enums.AudienceScopeEnum.CUSTOMER
    dimension = client.get_type("AudienceDimension")
    for entry in changes["segments"]:
        segment = client.get_type("AudienceSegment")
        if entry["kind"] == "custom_audience":
            segment.custom_audience.custom_audience = service.custom_audience_path(cid, entry["id"])
        else:
            segment.user_list.user_list = service.user_list_path(cid, entry["id"])
        dimension.audience_segments.segments.append(segment)
    audience.dimensions.append(dimension)
    ad_group = changes.get("ad_group")
    if ad_group:
        audience.resource_name = service.audience_path(cid, "-1")
    operations.append(op)

    if ad_group:
        op = client.get_type("MutateOperation")
        criterion = op.ad_group_criterion_operation.create
        criterion.ad_group = service.ad_group_path(cid, ad_group["ad_group_id"])
        criterion.audience.audience = service.audience_path(cid, "-1")
        operations.append(op)

    response = service.mutate(customer_id=cid, mutate_operations=operations)
    results = list(response.mutate_operation_responses)
    resource = results[0].audience_result.resource_name
    return {
        "audience": resource,
        "audience_id": resource.rsplit("/", 1)[-1],
        "segments": len(changes["segments"]),
        "ad_group_criterion": results[1].ad_group_criterion_result.resource_name if ad_group else None,
        "ad_group_id": ad_group["ad_group_id"] if ad_group else None,
    }
