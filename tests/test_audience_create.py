"""Demand Gen audience creation: custom segments, lookalike lists, audiences."""

from types import SimpleNamespace
from unittest.mock import patch

import pytest
from google.ads.googleads.client import GoogleAdsClient
from google.ads.googleads.v25.services.services.google_ads_service import GoogleAdsServiceClient

from adloop.ads import audience_create
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
    return AdLoopConfig(safety=SafetyConfig(log_file=str(tmp_path / "audit.log"), **safety))


_LISTS = [
    {"user_list.id": 11, "user_list.name": "Buyers 30d", "user_list.type": "RULE_BASED", "user_list.membership_status": "OPEN",
     "user_list.size_for_search": 4200, "user_list.size_for_display": 9000, "user_list.eligible_for_search": True,
     "user_list.eligible_for_display": True, "user_list.membership_life_span": 30, "user_list.account_user_list_status": "ENABLED"},
    {"user_list.id": 12, "user_list.name": "Newsletter", "user_list.type": "CRM_BASED", "user_list.membership_status": "CLOSED",
     "user_list.size_for_search": 10, "user_list.size_for_display": 40, "user_list.eligible_for_search": True,
     "user_list.eligible_for_display": True, "user_list.membership_life_span": 540, "user_list.account_user_list_status": "ENABLED"},
]
_CUSTOM = [{"custom_audience.id": 21, "custom_audience.name": "Competitor searchers", "custom_audience.type": "SEARCH"}]


def _gaql(channel="DEMAND_GEN", grouped=True, ad_groups=True):
    def fake(config, customer_id, query):
        if "FROM user_list" in query:
            return _LISTS
        if "FROM custom_audience" in query:
            return _CUSTOM
        if "FROM combined_audience" in query:
            return []
        if "FROM audience" in query:
            return [{"audience.id": 41, "audience.name": "Existing mix"}]
        if "FROM ad_group" in query:
            return [{"ad_group.id": 7, "ad_group.name": "DG prospects", "ad_group.audience_setting.use_audience_grouped": grouped,
                     "campaign.id": 5, "campaign.name": "DG IT", "campaign.advertising_channel_type": channel}] if ad_groups else []
        raise AssertionError(query)

    return patch("adloop.ads.gaql.execute_query", side_effect=fake)


def _details(result):
    return " ".join(result["details"])


class TestCustomSegmentDraft:
    def test_preview_normalizes_and_dedupes_members(self, tmp_path):
        with _gaql():
            preview = audience_create.draft_custom_segment(
                _config(tmp_path), customer_id="1", name="Running fans",
                urls=["runnersworld.com", "https://runnersworld.com", "http://strava.com/clubs"],
                keywords=["trail running", "Trail  Running", "marathon shoes"], segment_type="search",
            )
        changes = preview["changes"]
        assert preview["operation"] == "create_custom_segment"
        assert changes["urls"] == ["https://runnersworld.com", "http://strava.com/clubs"]
        assert changes["keywords"] == ["trail running", "marathon shoes"]
        assert changes["segment_type"] == "SEARCH"

    def test_inputs_are_validated(self, tmp_path):
        with _gaql():
            empty = audience_create.draft_custom_segment(_config(tmp_path), customer_id="1")
            bad = audience_create.draft_custom_segment(
                _config(tmp_path), customer_id="1", name="x", urls=["not a url", "ftp://x.com"],
                keywords=["one two three four five six seven eight nine ten eleven"], segment_type="INTEREST",
            )
            other = audience_create.draft_custom_segment(_config(tmp_path), customer_id="1", name="x", keywords=["a"], segment_type="FOO")
        assert "name is required" in _details(empty) and "at least one of urls or keywords" in _details(empty)
        for expected in ("'not a url' is not a valid", "'ftp://x.com' is not a valid", "is too long", "use AUTO"):
            assert expected in _details(bad)
        assert "segment_type must be one of" in _details(other)

    def test_duplicate_name_is_refused(self, tmp_path):
        with _gaql():
            result = audience_create.draft_custom_segment(_config(tmp_path), customer_id="1", name="competitor SEARCHERS", keywords=["a"])
        assert "already exists" in _details(result)

    def test_blocked_operation(self, tmp_path):
        result = audience_create.draft_custom_segment(
            _config(tmp_path, blocked_operations=["create_custom_segment"]), customer_id="1", name="x", keywords=["a"],
        )
        assert "error" in result and "create_custom_segment" in result["error"]


class TestLookalikeDraft:
    def test_preview_and_seed_warnings(self, tmp_path):
        with _gaql():
            preview = audience_create.draft_lookalike_list(
                _config(tmp_path), customer_id="1", name="Lookalike buyers", seed_user_list_ids=["12", "12"],
                expansion_level="narrow", country_codes=["it", "DE"],
            )
        changes = preview["changes"]
        assert preview["operation"] == "create_lookalike_list"
        assert changes["seed_user_list_ids"] == ["12"]
        assert changes["expansion_level"] == "NARROW"
        assert changes["country_codes"] == ["IT", "DE"]
        warnings = " ".join(preview["warnings"])
        assert "about 40 people" in warnings and "Closed seed lists" in warnings and "Demand Gen" in warnings

    def test_inputs_and_unknown_seeds_are_refused(self, tmp_path):
        bad = audience_create.draft_lookalike_list(
            _config(tmp_path), customer_id="1", seed_user_list_ids=["x"], expansion_level="HUGE", country_codes=["ITA"],
        )
        for expected in ("name is required", "must be numeric", "expansion_level must be one of", "alpha-2"):
            assert expected in _details(bad)
        with _gaql():
            unknown = audience_create.draft_lookalike_list(_config(tmp_path), customer_id="1", name="L", seed_user_list_ids=["99"])
            dup = audience_create.draft_lookalike_list(_config(tmp_path), customer_id="1", name="buyers 30d", seed_user_list_ids=["11"])
        assert "99 are not lists of this account" in _details(unknown)
        assert "already exists" in _details(dup)

    def test_blocked_operation(self, tmp_path):
        result = audience_create.draft_lookalike_list(
            _config(tmp_path, blocked_operations=["create_lookalike_list"]), customer_id="1", name="L", seed_user_list_ids=["11"],
        )
        assert "create_lookalike_list" in result["error"]


class TestAudienceDraft:
    def test_preview_with_ad_group(self, tmp_path):
        with _gaql():
            preview = audience_create.draft_audience(
                _config(tmp_path), customer_id="1", name="Runners mix", custom_audience_ids=["21"],
                user_list_ids=["11"], ad_group_id="7",
            )
        changes = preview["changes"]
        assert preview["operation"] == "create_audience"
        assert changes["segment_logic"] == "OR"
        assert [s["kind"] for s in changes["segments"]] == ["custom_audience", "user_list"]
        assert changes["ad_group"]["ad_group_id"] == "7"

    def test_unknown_ids_and_names_are_refused(self, tmp_path):
        with _gaql():
            result = audience_create.draft_audience(
                _config(tmp_path), customer_id="1", name="existing MIX", custom_audience_ids=["22"], user_list_ids=["98"],
            )
        details = _details(result)
        assert "already exists" in details and "22 are not custom segments" in details and "98 are not lists" in details
        empty = audience_create.draft_audience(_config(tmp_path), customer_id="1", name="x")
        assert "at least one of custom_audience_ids or user_list_ids" in _details(empty)

    def test_ad_group_must_be_grouped_demand_gen(self, tmp_path):
        kwargs = dict(customer_id="1", name="Mix", user_list_ids=["11"], ad_group_id="7")
        with _gaql(channel="DISPLAY"):
            display = audience_create.draft_audience(_config(tmp_path), **kwargs)
        with _gaql(grouped=False):
            ungrouped = audience_create.draft_audience(_config(tmp_path), **kwargs)
        with _gaql(ad_groups=False):
            missing = audience_create.draft_audience(_config(tmp_path), **kwargs)
        assert "only to Demand Gen ad groups" in _details(display)
        assert "use_audience_grouped" in _details(ungrouped)
        assert "was not found" in _details(missing)

    def test_blocked_operation(self, tmp_path):
        result = audience_create.draft_audience(
            _config(tmp_path, blocked_operations=["create_audience"]), customer_id="1", name="x", user_list_ids=["11"],
        )
        assert "create_audience" in result["error"]


class _FakeClient:
    """Real v25 proto types and path helpers; services record what was sent."""

    def __init__(self):
        base = GoogleAdsClient(credentials=None, developer_token="t", use_proto_plus=True, version=GOOGLE_ADS_API_VERSION)
        self.enums = base.enums
        self.get_type = base.get_type
        self.sent = {}

        def record(name, resource, field="operations"):
            def call(customer_id, **kwargs):
                self.sent[name] = list(kwargs[field])
                if field == "mutate_operations":
                    responses = [SimpleNamespace(audience_result=SimpleNamespace(resource_name=f"customers/{customer_id}/audiences/41"))]
                    if len(self.sent[name]) > 1:
                        responses.append(SimpleNamespace(
                            ad_group_criterion_result=SimpleNamespace(resource_name=f"customers/{customer_id}/adGroupCriteria/7~88")))
                    return SimpleNamespace(mutate_operation_responses=responses)
                return SimpleNamespace(results=[SimpleNamespace(resource_name=f"customers/{customer_id}/{resource}/77")])
            return call

        self._services = {
            "CustomAudienceService": SimpleNamespace(mutate_custom_audiences=record("custom", "customAudiences")),
            "UserListService": SimpleNamespace(mutate_user_lists=record("user_list", "userLists")),
            "GoogleAdsService": SimpleNamespace(
                audience_path=GoogleAdsServiceClient.audience_path,
                custom_audience_path=GoogleAdsServiceClient.custom_audience_path,
                user_list_path=GoogleAdsServiceClient.user_list_path,
                ad_group_path=GoogleAdsServiceClient.ad_group_path,
                mutate=record("mutate", "", "mutate_operations"),
            ),
        }

    def get_service(self, name):
        return self._services[name]


class TestApply:
    def test_custom_segment_members(self):
        client = _FakeClient()
        result = audience_create.apply_create_custom_segment(client, "123", {
            "name": "Running fans", "description": "d", "segment_type": "AUTO",
            "urls": ["https://runnersworld.com"], "keywords": ["trail running"],
        })
        (op,) = client.sent["custom"]
        audience = op.create
        assert audience.name == "Running fans" and audience.type_.name == "AUTO"
        assert [(m.member_type.name, m.url or m.keyword) for m in audience.members] == [
            ("URL", "https://runnersworld.com"), ("KEYWORD", "trail running")]
        assert result["custom_audience_id"] == "77" and result["members"] == 2

    def test_lookalike_fields(self):
        client = _FakeClient()
        result = audience_create.apply_create_lookalike_list(client, "123", {
            "name": "LAL", "description": None, "seed_user_list_ids": ["11", "12"],
            "expansion_level": "BROAD", "country_codes": ["IT"],
        })
        (op,) = client.sent["user_list"]
        lookalike = op.create.lookalike_user_list
        assert list(lookalike.seed_user_list_ids) == [11, 12]
        assert lookalike.expansion_level.name == "BROAD"
        assert list(lookalike.country_codes) == ["IT"]
        assert result == {"user_list": "customers/123/userLists/77", "user_list_id": "77", "next_steps": result["next_steps"]}

    def test_audience_segments_and_ad_group_criterion_in_one_mutate(self):
        client = _FakeClient()
        result = audience_create.apply_create_audience(client, "123", {
            "name": "Mix", "description": None,
            "segments": [{"kind": "custom_audience", "id": "21", "name": "c"}, {"kind": "user_list", "id": "11", "name": "u"}],
            "ad_group": {"ad_group_id": "7"},
        })
        audience_op, criterion_op = client.sent["mutate"]
        audience = audience_op.audience_operation.create
        assert audience.resource_name == "customers/123/audiences/-1"
        assert audience.scope.name == "CUSTOMER"
        (dimension,) = audience.dimensions
        segments = dimension.audience_segments.segments
        assert segments[0].custom_audience.custom_audience == "customers/123/customAudiences/21"
        assert segments[1].user_list.user_list == "customers/123/userLists/11"
        criterion = criterion_op.ad_group_criterion_operation.create
        assert criterion.ad_group == "customers/123/adGroups/7"
        assert criterion.audience.audience == "customers/123/audiences/-1"
        assert result["audience_id"] == "41" and result["ad_group_criterion"] == "customers/123/adGroupCriteria/7~88"

    def test_audience_without_ad_group(self):
        client = _FakeClient()
        result = audience_create.apply_create_audience(client, "123", {
            "name": "Mix", "segments": [{"kind": "user_list", "id": "11", "name": "u"}], "ad_group": None,
        })
        (op,) = client.sent["mutate"]
        assert not op.audience_operation.create.resource_name
        assert result["ad_group_criterion"] is None

    def test_dry_run_wrapper_validates_the_same_calls(self):
        from adloop.ads.validate_only import ValidateOnlyClient

        class _Raw(_FakeClient):
            def __init__(self):
                super().__init__()
                base = GoogleAdsClient(credentials=None, developer_token="t", use_proto_plus=True, version=GOOGLE_ADS_API_VERSION)
                requests = self.requests = []

                def target(request):
                    requests.append(request)
                    return SimpleNamespace(partial_failure_error=None)

                self._services["UserListService"] = SimpleNamespace(mutate_user_lists=target)
                self._services["GoogleAdsService"].mutate = target
                self.get_type = base.get_type

        raw = _Raw()
        wrapped = ValidateOnlyClient(raw)
        audience_create.apply_create_lookalike_list(wrapped, "123", {
            "name": "LAL", "seed_user_list_ids": ["11"], "expansion_level": "BALANCED", "country_codes": [],
        })
        audience_create.apply_create_audience(wrapped, "123", {
            "name": "Mix", "segments": [{"kind": "user_list", "id": "11", "name": "u"}], "ad_group": {"ad_group_id": "7"},
        })
        assert wrapped.validated_calls == 2 and wrapped.skipped_calls == 0
        assert all(r.validate_only for r in raw.requests)
