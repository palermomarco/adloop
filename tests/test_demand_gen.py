"""Demand Gen campaigns and multi-asset ads: draft checks and the mutates."""

from types import SimpleNamespace
from unittest.mock import patch

import pytest
from google.ads.googleads.client import GoogleAdsClient
from google.ads.googleads.v25.services.services.google_ads_service import GoogleAdsServiceClient

from adloop.ads import demand_gen
from adloop.ads.client import GOOGLE_ADS_API_VERSION
from adloop.config import AdLoopConfig, SafetyConfig
from adloop.safety import preview as preview_store
from adloop.safety.preview import InMemoryPlanStore


@pytest.fixture(autouse=True)
def _plans():
    preview_store.set_plan_store(InMemoryPlanStore())
    yield
    preview_store.set_plan_store(InMemoryPlanStore())


def _config(tmp_path, **safety) -> AdLoopConfig:
    return AdLoopConfig(safety=SafetyConfig(log_file=str(tmp_path / "audit.log"), max_daily_budget=100, **safety))


def _campaign(tmp_path, **overrides):
    kwargs = dict(
        customer_id="1234567890", campaign_name="DG IT", daily_budget=20,
        geo_target_ids=["2380"], language_ids=["1004"],
    )
    kwargs.update(overrides)
    return demand_gen.draft_demand_gen_campaign(_config(tmp_path), **kwargs)


class _FakeClient:
    """Real v25 proto types and path helpers, a recording GoogleAdsService."""

    def __init__(self):
        base = GoogleAdsClient(credentials=None, developer_token="t", use_proto_plus=True, version=GOOGLE_ADS_API_VERSION)
        self.enums = base.enums
        self.get_type = base.get_type
        self.sent = []

        def mutate(customer_id, mutate_operations):
            self.sent = list(mutate_operations)
            return SimpleNamespace(mutate_operation_responses=[])

        self._service = SimpleNamespace(
            campaign_budget_path=GoogleAdsServiceClient.campaign_budget_path,
            campaign_path=GoogleAdsServiceClient.campaign_path,
            ad_group_path=GoogleAdsServiceClient.ad_group_path,
            asset_path=GoogleAdsServiceClient.asset_path,
            mutate=mutate,
        )

    def get_service(self, name):
        assert name == "GoogleAdsService"
        return self._service


class TestCampaignDraft:
    def test_preview_is_paused_with_safe_defaults(self, tmp_path):
        preview = _campaign(tmp_path, channels=["Discover", "gmail"], start_date="2099-01-01", end_date="2099-01-31")
        changes = preview["changes"]
        assert preview["operation"] == "create_demand_gen_campaign"
        assert changes["status_on_create"] == "PAUSED"
        assert changes["bidding_strategy"] == "MAXIMIZE_CONVERSIONS"
        assert changes["optimized_targeting"] is False
        assert changes["channels"] == ["discover", "gmail"]
        assert changes["ad_group_name"] == "DG IT"
        assert (changes["start_date"], changes["end_date"]) == ("2099-01-01", "2099-01-31")

    def test_no_channels_means_all_and_is_warned(self, tmp_path):
        preview = _campaign(tmp_path)
        assert preview["changes"]["channels"] == "ALL_CHANNELS"
        assert any("Display Network" in w for w in preview["warnings"])

    def test_inputs_are_validated(self, tmp_path):
        result = _campaign(
            tmp_path, campaign_name="", daily_budget=0, bidding_strategy="MANUAL_CPC", geo_target_ids=[],
            language_ids=["it"], channels=["tiktok"], start_date="2099-02-01", end_date="2099-01-01",
        )
        details = " ".join(result["details"])
        for expected in ("campaign_name is required", "daily_budget must be", "bidding_strategy must be one of",
                         "geo_target_ids is required", "language_ids must be numeric", "channels must be among",
                         "is before start_date"):
            assert expected in details
        assert any("target_cpa is required" in d for d in _campaign(tmp_path, bidding_strategy="TARGET_CPA")["details"])
        assert any("max_cpc only applies" in d for d in _campaign(tmp_path, max_cpc=1)["details"])

    def test_budget_cap_and_blocked_operation(self, tmp_path):
        assert "500" in _campaign(tmp_path, daily_budget=500)["error"]
        blocked = demand_gen.draft_demand_gen_campaign(
            _config(tmp_path, blocked_operations=["create_demand_gen_campaign"]), campaign_name="x"
        )
        assert "blocked" in blocked["error"]


class TestCampaignMutate:
    def _ops(self, **overrides):
        changes = {
            "campaign_name": "DG IT", "daily_budget": 20, "bidding_strategy": "TARGET_CPA", "target_cpa": 12.5,
            "max_cpc": None, "start_date": "2099-01-01", "end_date": "2099-01-31", "ad_group_name": "Group",
            "geo_target_ids": ["2380"], "language_ids": ["1004", "1000"], "channels": ["discover", "gmail"],
            "optimized_targeting": False,
        }
        changes.update(overrides)
        client = _FakeClient()
        return client, demand_gen.build_demand_gen_campaign_operations(client, "1234567890", changes)

    def test_operations_follow_googles_flow(self):
        client, ops = self._ops()
        assert [op._pb.WhichOneof("operation") for op in ops] == [
            "campaign_budget_operation", "campaign_operation", "ad_group_operation",
            "ad_group_criterion_operation", "ad_group_criterion_operation", "ad_group_criterion_operation",
        ]
        assert ops[0].campaign_budget_operation.create.explicitly_shared is False
        assert ops[0].campaign_budget_operation.create.amount_micros == 20_000_000

        campaign = ops[1].campaign_operation.create
        assert campaign.advertising_channel_type == client.enums.AdvertisingChannelTypeEnum.DEMAND_GEN
        assert campaign.status == client.enums.CampaignStatusEnum.PAUSED
        assert campaign.target_cpa.target_cpa_micros == 12_500_000
        assert campaign.start_date_time == "2099-01-01 00:00:00"
        assert campaign.end_date_time == "2099-01-31 23:59:59"

        ad_group = ops[2].ad_group_operation.create
        assert ad_group.campaign == campaign.resource_name
        # Demand Gen ad groups are created without a type.
        assert ad_group.type_ == client.enums.AdGroupTypeEnum.UNSPECIFIED
        assert ad_group.audience_setting.use_audience_grouped is True
        selected = ad_group.demand_gen_ad_group_settings.channel_controls.selected_channels
        assert (selected.discover, selected.gmail, selected.display, selected.youtube_shorts) == (True, True, False, False)

        # Location and language live on the ad group, not the campaign.
        criteria = [op.ad_group_criterion_operation.create for op in ops[3:]]
        assert all(c.ad_group == ad_group.resource_name for c in criteria)
        assert criteria[0].location.geo_target_constant == "geoTargetConstants/2380"
        assert [c.language.language_constant for c in criteria[1:]] == ["languageConstants/1004", "languageConstants/1000"]

    def test_bidding_and_all_channels(self):
        _, ops = self._ops(bidding_strategy="TARGET_SPEND", max_cpc=0.8, channels="ALL_CHANNELS", end_date=None)
        campaign = ops[1].campaign_operation.create
        assert campaign.target_spend.cpc_bid_ceiling_micros == 800_000
        assert not campaign._pb.HasField("end_date_time")
        assert not ops[2].ad_group_operation.create._pb.HasField("demand_gen_ad_group_settings")

        _, ops = self._ops(optimized_targeting=True)
        assert ops[2].ad_group_operation.create.optimized_targeting_enabled is True

    def test_optimized_targeting_is_switched_off_after_the_create(self):
        changes = {
            "campaign_name": "DG IT", "daily_budget": 20, "bidding_strategy": "TARGET_SPEND", "ad_group_name": "G",
            "geo_target_ids": ["2380"], "language_ids": ["1004"], "channels": "ALL_CHANNELS",
        }
        client = _FakeClient()
        calls = []

        def mutate(customer_id, mutate_operations):
            calls.append(list(mutate_operations))
            names = ["customers/1/campaignBudgets/7", "customers/1/campaigns/8", "customers/1/adGroups/9"]
            if len(calls) == 2:
                raise RuntimeError("boom")
            return SimpleNamespace(mutate_operation_responses=names)

        client._service.mutate = mutate
        with patch("adloop.ads.write._extract_resource_name", side_effect=lambda r: r):
            result = demand_gen.apply_create_demand_gen_campaign(client, "1", changes)
        off = calls[1][0].ad_group_operation
        assert off.update.resource_name == "customers/1/adGroups/9" and not off.update.optimized_targeting_enabled
        assert list(off.update_mask.paths) == ["optimized_targeting_enabled"]
        # The campaign exists, so the failed follow-up is reported, not raised.
        assert result["campaign"] == "customers/1/campaigns/8"
        assert "NOT VERIFIED OFF" in result["optimized_targeting"]

        _, ops = self._ops(bidding_strategy="MAXIMIZE_CONVERSIONS")
        assert ops[1].campaign_operation.create._pb.HasField("maximize_conversions")


_LIBRARY = {
    "11": {"width": 1200, "height": 628},
    "12": {"width": 1200, "height": 1200},
    "13": {"width": 960, "height": 1200},
    "14": {"width": 512, "height": 512},
}


def _ad(tmp_path, url_error=None, **overrides):
    kwargs = dict(
        customer_id="1234567890", ad_group_id="555", final_url="https://example.com/",
        headlines=["Headline one"], descriptions=["A description"], business_name="Example Co",
        marketing_image_asset_ids=["11"], square_image_asset_ids=["12"], portrait_image_asset_ids=["13"],
        logo_asset_ids=["14"], call_to_action="Scopri di più",
    )
    kwargs.update(overrides)
    with patch.object(demand_gen, "_library_images", return_value=_LIBRARY), patch(
        "adloop.ads.write._validate_urls", return_value=({kwargs["final_url"]: url_error}, {})
    ):
        return demand_gen.draft_demand_gen_ad(_config(tmp_path), **kwargs)


class TestAdDraft:
    def test_preview_is_paused(self, tmp_path):
        preview = _ad(tmp_path)
        assert preview["operation"] == "create_demand_gen_ad"
        assert preview["changes"]["status_on_create"] == "PAUSED"
        assert preview["changes"]["logo_asset_ids"] == ["14"]

    def test_text_limits(self, tmp_path):
        result = _ad(
            tmp_path, headlines=["x" * 41] + ["h"] * 5, descriptions=["y" * 91], business_name="z" * 26,
        )
        details = " ".join(result["details"])
        for expected in ("has 41 characters (max 40)", "headlines: 1 to 5 required, got 6",
                         "has 91 characters (max 90)", "business_name has 26 characters (max 25)",
                         "headlines must be unique"):
            assert expected in details
        assert _ad(tmp_path, headlines=["x" * 40], descriptions=["y" * 90], business_name="z" * 25).get("plan_id")

    def test_images_are_required_and_checked_against_the_library(self, tmp_path):
        missing = _ad(tmp_path, marketing_image_asset_ids=[], square_image_asset_ids=[], logo_asset_ids=[])
        details = " ".join(missing["details"])
        assert "at least one landscape" in details and "logo_asset_ids: 1 to 5 required" in details

        wrong = _ad(tmp_path, marketing_image_asset_ids=["12"], logo_asset_ids=["99"])
        details = " ".join(wrong["details"])
        assert "asset 12 is 1200x1200, this slot needs a 1.91:1 image" in details
        assert "asset 99 is not an image asset of this account" in details

    def test_unreachable_final_url_is_refused(self, tmp_path):
        assert "final_url: HTTP 404" in _ad(tmp_path, url_error="HTTP 404")["details"][0]


def test_ad_mutate_builds_a_paused_multi_asset_ad():
    client = _FakeClient()
    demand_gen.apply_create_demand_gen_ad(client, "1234567890", {
        "ad_group_id": "555", "ad_name": "Ad 1", "final_url": "https://example.com/",
        "headlines": ["H1", "H2"], "descriptions": ["D1"], "business_name": "Example Co",
        "marketing_image_asset_ids": ["11"], "square_image_asset_ids": ["12"], "portrait_image_asset_ids": ["13"],
        "logo_asset_ids": ["14"], "call_to_action": "Scopri di più",
    })
    (op,) = client.sent
    ad_group_ad = op.ad_group_ad_operation.create
    assert ad_group_ad.ad_group == "customers/1234567890/adGroups/555"
    assert ad_group_ad.status == client.enums.AdGroupAdStatusEnum.PAUSED
    assert list(ad_group_ad.ad.final_urls) == ["https://example.com/"]
    info = ad_group_ad.ad.demand_gen_multi_asset_ad
    assert [h.text for h in info.headlines] == ["H1", "H2"]
    assert info.business_name == "Example Co" and info.call_to_action_text == "Scopri di più"
    assert [i.asset for i in info.marketing_images] == ["customers/1234567890/assets/11"]
    assert [i.asset for i in info.square_marketing_images] == ["customers/1234567890/assets/12"]
    assert [i.asset for i in info.portrait_marketing_images] == ["customers/1234567890/assets/13"]
    assert [i.asset for i in info.logo_images] == ["customers/1234567890/assets/14"]



class TestGeneratedMedia:
    def test_ad_draft_defaults_to_off(self, tmp_path):
        changes = _ad(tmp_path)["changes"]
        assert (changes["generated_videos"], changes["animated_images"]) == (False, False)

    def test_ad_create_opts_out_unless_asked(self):
        client = _FakeClient()
        demand_gen.apply_create_demand_gen_ad(client, "1", {
            "ad_group_id": "555", "final_url": "https://example.com/", "headlines": ["H"], "descriptions": ["D"],
            "business_name": "Example Co", "square_image_asset_ids": ["12"], "logo_asset_ids": ["14"],
            "generated_videos": False, "animated_images": True,
        })
        ad_group_ad = client.sent[0].ad_group_ad_operation.create
        settings = {
            client.enums.AssetAutomationTypeEnum(s.asset_automation_type).name:
                client.enums.AssetAutomationStatusEnum(s.asset_automation_status).name
            for s in ad_group_ad.ad_group_ad_asset_automation_settings
        }
        assert settings == {
            "GENERATE_VIDEOS_FROM_OTHER_ASSETS": "OPTED_OUT",
            "GENERATE_ANIMATED_IMAGES_FROM_OTHER_ASSETS": "OPTED_IN",
        }
