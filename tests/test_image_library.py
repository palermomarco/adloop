"""Library image assets: draft checks and the unlinked asset mutate."""

import struct
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from google.ads.googleads.client import GoogleAdsClient

from adloop import uploads
from adloop.ads import image_library
from adloop.ads.client import GOOGLE_ADS_API_VERSION
from adloop.ads.validate_only import PLACEHOLDER, ValidateOnlyClient
from adloop.config import AdLoopConfig, SafetyConfig
from adloop.safety import preview as preview_store
from adloop.safety.preview import InMemoryPlanStore
from adloop.uploads import LocalUploadStore


def _png(width, height, salt=b""):
    return b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR" + struct.pack(">II", width, height) + b"\x08\x02\x00\x00\x00" + salt


@pytest.fixture(autouse=True)
def _env(tmp_path):
    preview_store.set_plan_store(InMemoryPlanStore())
    uploads.set_upload_store(LocalUploadStore(tmp_path / "uploads"))
    yield
    uploads.set_upload_store(None)
    preview_store.set_plan_store(InMemoryPlanStore())


def _config(tmp_path, **safety):
    return AdLoopConfig(safety=SafetyConfig(log_file=str(tmp_path / "audit.log"), **safety))


def _uploads(*sizes):
    store = uploads.get_upload_store()
    return [store.put("local", f"img{i}.png", _png(w, h, bytes([i]))).upload_id for i, (w, h) in enumerate(sizes)]


def _draft(tmp_path, **kwargs):
    return image_library.draft_library_image_assets(_config(tmp_path), customer_id="1234567890", **kwargs)


def test_no_image_is_refused(tmp_path):
    result = _draft(tmp_path)
    assert result["error"] == "Validation failed"
    assert "At least one image" in result["details"][0]


def test_images_are_classified_by_ratio(tmp_path):
    ids = _uploads((1200, 628), (1200, 1200), (960, 1200), (1080, 1920), (1000, 700))
    preview = _draft(tmp_path, upload_ids=ids)
    changes = preview["changes"]
    assert preview["operation"] == "create_library_image_assets"
    assert preview["entity_type"] == "asset"
    assert [i["ratio"] for i in changes["images"]] == ["landscape", "square", "portrait", "tall_portrait", "other"]
    assert changes["summary"][0].endswith("(landscape)")
    assert preview["warnings"] == [
        "Image 5: 1000x700 fits no Demand Gen image slot (1.91:1, 1:1, 4:5 or 9:16); crop it before using it in an ad"
    ]


def test_small_images_are_warned_not_refused(tmp_path):
    ids = _uploads((500, 262), (200, 200), (100, 100))
    preview = _draft(tmp_path, upload_ids=ids)
    assert [i["ratio"] for i in preview["changes"]["images"]] == ["landscape", "square", "square"]
    warnings = " ".join(preview["warnings"])
    assert "500x262 is below the 600x314 minimum for a landscape" in warnings
    assert "200x200 is below 300x300 for a square marketing image; usable only as a logo" in warnings
    assert "100x100 is below the 300x300 minimum for a square" in warnings


def test_blocked_operation_and_server_paths(tmp_path):
    blocked = image_library.draft_library_image_assets(
        _config(tmp_path, blocked_operations=["create_library_image_assets"]), upload_ids=_uploads((300, 300)),
    )
    assert "blocked" in blocked["error"]
    with patch("adloop.runtime.deployment_mode", return_value="server"):
        assert "image_paths can't be used here" in _draft(tmp_path, image_paths=["/tmp/x.png"])["error"]


class _FakeClient:
    def __init__(self):
        base = GoogleAdsClient(credentials=None, developer_token="t", use_proto_plus=True, version=GOOGLE_ADS_API_VERSION)
        self.enums, self.get_type = base.enums, base.get_type
        self.requests = []

        def mutate(request=None, customer_id="", mutate_operations=()):
            self.requests.append(request or SimpleNamespace(customer_id=customer_id, mutate_operations=mutate_operations))
            ops = request.mutate_operations if request else mutate_operations
            return SimpleNamespace(mutate_operation_responses=[
                SimpleNamespace(asset_result=SimpleNamespace(resource_name=f"customers/1/assets/{100 + i}"))
                for i in range(len(ops))
            ])

        self._service = SimpleNamespace(mutate=mutate)

    def get_service(self, name):
        assert name == "GoogleAdsService"
        return self._service


def test_apply_creates_unlinked_assets_and_returns_ids(tmp_path):
    preview = _draft(tmp_path, upload_ids=_uploads((1200, 628), (128, 128)))
    client = _FakeClient()
    result = image_library.apply_create_library_image_assets(client, "1", preview["changes"])

    ops = list(client.requests[0].mutate_operations)
    assert [op._pb.WhichOneof("operation") for op in ops] == ["asset_operation", "asset_operation"]
    asset = ops[0].asset_operation.create
    assert asset.type_ == client.enums.AssetTypeEnum.IMAGE
    assert asset.image_asset.mime_type == client.enums.MimeTypeEnum.IMAGE_PNG
    assert asset.image_asset.full_size.width_pixels == 1200
    assert asset.name == preview["changes"]["images"][0]["name"]
    assert result["assets"] == [
        {"resource_name": "customers/1/assets/100", "asset_id": "100", "name": asset.name,
         "width": 1200, "height": 628, "ratio": "landscape"},
        {"resource_name": "customers/1/assets/101", "asset_id": "101", "name": ops[1].asset_operation.create.name,
         "width": 128, "height": 128, "ratio": "square"},
    ]


def test_apply_runs_validate_only(tmp_path):
    preview = _draft(tmp_path, upload_ids=_uploads((1200, 1200)))
    fake = _FakeClient()
    client = ValidateOnlyClient(fake)
    result = image_library.apply_create_library_image_assets(client, "1", preview["changes"])
    assert fake.requests[0].validate_only is True
    assert client.validated_calls == 1
    assert PLACEHOLDER in result["assets"][0]["resource_name"]
