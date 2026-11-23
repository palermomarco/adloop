"""Campaign end date on update_campaign.

None leaves the end date unchanged, "YYYY-MM-DD" sets the last day the campaign
runs, "" removes it. In API v23+ the field is Campaign.end_date_time
("yyyy-MM-dd HH:mm:ss" in the account time zone).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from adloop.ads import write
from adloop.config import AdLoopConfig, AdsConfig, SafetyConfig
from adloop.safety import preview as preview_store
from tests.test_ads_write import (
    _FakeClient,
    _FakeGoogleAdsService,
    _FakeMutateOperationResponse,
    _FakePathService,
)

CAMPAIGN = "1001"
FUTURE = (datetime.now(UTC).date() + timedelta(days=30)).isoformat()
PAST = (datetime.now(UTC).date() - timedelta(days=1)).isoformat()


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


def _current(monkeypatch, *, end_date_time="", found=True):
    queries: list[str] = []

    def execute_query(_config, _cid, query):
        queries.append(query)
        if not found:
            return []
        return [{"campaign.end_date_time": end_date_time}]

    monkeypatch.setattr("adloop.ads.gaql.execute_query", execute_query)
    return queries


def _update(config, **kwargs):
    return write.update_campaign(config, customer_id="1234567890", campaign_id=CAMPAIGN, **kwargs)


class TestDraft:
    def test_sets_an_end_date(self, config, monkeypatch):
        queries = _current(monkeypatch)

        result = _update(config, end_date=FUTURE)

        assert result["changes"]["end_date"] == FUTURE
        assert result["changes"]["current_end_date"] == ""
        assert any("stops serving after" in w for w in result["warnings"])
        assert "campaign.end_date_time" in queries[0]

    def test_removes_the_end_date(self, config, monkeypatch):
        _current(monkeypatch, end_date_time="2030-06-30 23:59:59")

        result = _update(config, end_date="")

        assert result["changes"]["end_date"] == ""
        assert result["changes"]["current_end_date"] == "2030-06-30"
        assert any("runs indefinitely" in w for w in result["warnings"])

    def test_same_date_is_nothing_to_change(self, config, monkeypatch):
        _current(monkeypatch, end_date_time=f"{FUTURE} 23:59:59")

        assert _update(config, end_date=FUTURE) == {
            "error": f"Campaign {CAMPAIGN} already has these values; nothing to change"
        }

    def test_removing_when_there_is_none_is_nothing_to_change(self, config, monkeypatch):
        _current(monkeypatch)

        assert "nothing to change" in _update(config, end_date="")["error"]

    @pytest.mark.parametrize("value", ["2030-13-01", "30/06/2030", "tomorrow"])
    def test_rejects_invalid_dates(self, config, monkeypatch, value):
        _current(monkeypatch)

        result = _update(config, end_date=value)

        assert result["error"] == "Validation failed"
        assert any("YYYY-MM-DD" in d for d in result["details"])

    def test_rejects_a_past_date(self, config, monkeypatch):
        _current(monkeypatch)

        result = _update(config, end_date=PAST)

        assert any("in the past" in d for d in result["details"])

    def test_unknown_campaign(self, config, monkeypatch):
        _current(monkeypatch, found=False)

        assert _update(config, end_date=FUTURE) == {
            "error": f"Campaign {CAMPAIGN} was not found in this account"
        }


def _client(service):
    return _FakeClient({
        "GoogleAdsService": service,
        "CampaignService": _FakePathService("campaigns"),
        "CampaignBudgetService": _FakePathService("campaignBudgets"),
    })


class TestApply:
    def _apply(self, end_date):
        service = _FakeGoogleAdsService(
            [_FakeMutateOperationResponse("campaign_result", "customers/1/campaigns/1")]
        )
        write._apply_update_campaign(_client(service), "1234567890", {
            "campaign_id": CAMPAIGN,
            "current_end_date": "",
            "end_date": end_date,
        })
        return service.operations[0].campaign_operation

    def test_sets_end_of_day(self):
        operation = self._apply("2030-06-30")

        assert list(operation.update_mask.paths) == ["end_date_time"]
        assert operation.update.end_date_time == "2030-06-30 23:59:59"

    def test_clears_under_the_mask(self):
        operation = self._apply("")

        assert list(operation.update_mask.paths) == ["end_date_time"]
        assert "end_date_time" not in operation.update


@pytest.mark.asyncio
async def test_tool_documents_end_date():
    from adloop.server import mcp

    tools = {t.name: t for t in await mcp.list_tools()}
    assert "end_date" in tools["update_campaign"].parameters["properties"]
