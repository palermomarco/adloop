"""update_campaign generated_videos / animated_images: the multi-asset ads of a
Demand Gen campaign (Google keeps the setting per AdGroupAd)."""

from __future__ import annotations

import pytest

from adloop.ads import write
from adloop.config import AdLoopConfig, AdsConfig, SafetyConfig
from adloop.safety import preview as preview_store
from tests.test_ads_write import _FakeGoogleAdsService, _FakeMutateOperationResponse
from tests.test_campaign_end_date import _client

VIDEOS = "GENERATE_VIDEOS_FROM_OTHER_ASSETS"
ANIMATED = "GENERATE_ANIMATED_IMAGES_FROM_OTHER_ASSETS"
DESIGN = "GENERATE_DESIGN_VERSIONS_FOR_IMAGES"


@pytest.fixture(autouse=True)
def _plans():
    preview_store.set_plan_store(preview_store.InMemoryPlanStore())
    yield
    preview_store.set_plan_store(preview_store.InMemoryPlanStore())


@pytest.fixture
def config(tmp_path) -> AdLoopConfig:
    return AdLoopConfig(
        ads=AdsConfig(customer_id="1234567890"),
        safety=SafetyConfig(log_file=str(tmp_path / "audit.log"), max_daily_budget=500),
    )


def _account(monkeypatch, channel="DEMAND_GEN", ads=None):
    def execute_query(_config, _cid, query):
        if "FROM ad_group_ad" in query:
            return ads if ads is not None else [
                {"ad_group.id": 7, "ad_group_ad.ad.id": 70, "ad_group_ad.ad.name": "a",
                 "ad_group_ad.ad_group_ad_asset_automation_settings": [
                     {"asset_automation_type": DESIGN, "asset_automation_status": "OPTED_IN"}]},
                {"ad_group.id": 8, "ad_group_ad.ad.id": 80, "ad_group_ad.ad.name": "b",
                 "ad_group_ad.ad_group_ad_asset_automation_settings": [
                     {"asset_automation_type": VIDEOS, "asset_automation_status": "OPTED_OUT"},
                     {"asset_automation_type": ANIMATED, "asset_automation_status": "OPTED_OUT"}]},
            ]
        return [{"campaign.advertising_channel_type": channel}]

    monkeypatch.setattr("adloop.ads.gaql.execute_query", execute_query)


def _update(config, **kwargs):
    return write.update_campaign(config, customer_id="1234567890", campaign_id="1001", **kwargs)


def test_only_ads_that_differ_change_and_other_automations_stay(config, monkeypatch):
    _account(monkeypatch)

    changes = _update(config, generated_videos=False, animated_images=False)["changes"]

    (ad,) = changes["generated_media_ads"]  # ad 80 is already off
    assert (ad["ad_group_id"], ad["ad_id"]) == ("7", "70")
    assert ad["current"] == {"generated_videos": True, "animated_images": True}
    assert {s["asset_automation_type"]: s["asset_automation_status"] for s in ad["settings"]} == {
        DESIGN: "OPTED_IN", VIDEOS: "OPTED_OUT", ANIMATED: "OPTED_OUT",
    }


def test_nothing_to_change_and_refusals(config, monkeypatch):
    _account(monkeypatch, ads=[])
    assert "no multi-asset ads" in _update(config, generated_videos=False)["error"]

    _account(monkeypatch, channel="SEARCH")
    assert "Demand Gen campaigns only" in _update(config, generated_videos=False)["error"]

    _account(monkeypatch, ads=[{"ad_group.id": 8, "ad_group_ad.ad.id": 80, "ad_group_ad.ad_group_ad_asset_automation_settings": [
        {"asset_automation_type": VIDEOS, "asset_automation_status": "OPTED_OUT"}]}])
    assert "nothing to change" in _update(config, generated_videos=False)["error"]


def test_apply_updates_each_ad_under_the_mask():
    service = _FakeGoogleAdsService([_FakeMutateOperationResponse("ad_group_result", "customers/1/adGroupAds/7~70")])
    service.ad_group_ad_path = lambda cid, ad_group_id, ad_id: f"customers/{cid}/adGroupAds/{ad_group_id}~{ad_id}"
    write._apply_update_campaign(_client(service), "1234567890", {
        "campaign_id": "1001",
        "generated_media_ads": [{"ad_group_id": "7", "ad_id": "70", "settings": [
            {"asset_automation_type": VIDEOS, "asset_automation_status": "OPTED_OUT"}]}],
    })
    operation = service.operations[0].ad_group_ad_operation

    assert operation.update.resource_name.endswith("adGroupAds/7~70")
    assert list(operation.update_mask.paths) == ["ad_group_ad_asset_automation_settings"]
    assert len(operation.update.ad_group_ad_asset_automation_settings) == 1
