"""Unit tests for Magento product payload helpers."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict

import pytest

import sys

sys.path.append(str(Path(__file__).resolve().parents[1]))

# Provide dummy configuration values so importing the Magento client does not
# attempt to load Streamlit secrets during tests.
os.environ.setdefault("MAGENTO_BASE_URL", "https://example.test")
os.environ.setdefault("MAGENTO_ADMIN_TOKEN", "test-token")
os.environ.setdefault("OPENAI_API_KEY", "test-key")

from services.apply import apply_product_update, build_product_payload


@pytest.fixture(autouse=True)
def mock_attribute_metadata(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(
        "services.apply.client.get_attribute",
        lambda code: {"attribute_code": code},
    )
    monkeypatch.setattr("services.apply.client.get_attribute_options", lambda code: [])


def _capture_patch(monkeypatch: pytest.MonkeyPatch):
    """Return a helper capturing payloads sent to the Magento client."""

    captured: Dict[str, Any] = {}

    def fake_patch(endpoint: str, payload: Dict[str, Any]):
        captured["endpoint"] = endpoint
        captured["payload"] = payload
        return {"status": "ok"}

    monkeypatch.setattr("services.apply.client.patch", fake_patch)
    return captured


@pytest.mark.parametrize(
    "value",
    ["__no_update__", "no_update", "   ", ""],
)
def test_custom_layout_update_file_sentinels_are_dropped(value: str):
    diff, _, payload = build_product_payload(
        "SKU123",
        {"custom_layout_update_file": value},
    )

    assert diff == {}
    assert payload == {}


def test_custom_layout_update_file_passes_through_real_path():
    diff, _, payload = build_product_payload(
        "SKU123",
        {"custom_layout_update_file": "Magento/theme/layout.xml"},
    )

    assert diff == {"custom_layout_update_file": "Magento/theme/layout.xml"}
    assert payload == {
        "product": {
            "sku": "SKU123",
            "custom_attributes": [
                {
                    "attribute_code": "custom_layout_update_file",
                    "value": "Magento/theme/layout.xml",
                }
            ],
        }
    }


def test_only_changed_attributes_are_sent():
    diff, _, payload = build_product_payload(
        "SKU123",
        {"options_container": "container2", "color": "5"},
        {
            "options_container": {"input": "select"},
            "color": {"input": "select"},
        },
        original_attributes={
            "options_container": "container2",
            "color": "4",
        },
    )

    assert diff == {"color": 5}
    assert payload == {
        "product": {
            "sku": "SKU123",
            "custom_attributes": [
                {"attribute_code": "color", "value": 5},
            ],
        }
    }


def test_static_attributes_and_media_are_dropped():
    diff, _, payload = build_product_payload(
        "SKU123",
        {
            "has_options": 1,
            "required_options": 1,
            "image": "image.jpg",
            "small_image": "image.jpg",
            "thumbnail": "image.jpg",
        },
    )

    assert diff == {}
    assert payload == {}


def test_description_is_sanitised():
    diff, _, payload = build_product_payload(
        "SKU123",
        {"description": r"Line\\break \\value &amp;"},
    )

    assert diff == {"description": "Linebreak value &"}
    assert payload == {
        "product": {
            "sku": "SKU123",
            "custom_attributes": [
                {"attribute_code": "description", "value": "Linebreak value &"},
            ],
        }
    }


def test_category_links_are_strings():
    diff, _, payload = build_product_payload(
        "SKU123",
        {"category_ids": [1, 2]},
        original_extension_attributes={
            "category_links": [
                {"category_id": "1"},
            ]
        },
    )

    assert diff == {"category_ids": [1, 2]}
    assert payload == {
        "product": {
            "sku": "SKU123",
            "custom_attributes": [],
            "extension_attributes": {
                "category_links": [
                    {"category_id": "1"},
                    {"category_id": "2"},
                ]
            },
        }
    }


def test_category_links_not_sent_when_unchanged():
    diff, _, payload = build_product_payload(
        "SKU123",
        {"category_ids": [3]},
        original_extension_attributes={
            "category_links": [
                {"category_id": "3"},
            ]
        },
    )

    assert diff == {}
    assert payload == {}


def test_apply_product_update_regenerates_payload(monkeypatch: pytest.MonkeyPatch):
    captured = _capture_patch(monkeypatch)

    def fake_get_product(_):
        return {
            "sku": "SKU123",
            "custom_attributes": [
                {
                    "attribute_code": "custom_layout_update_file",
                    "value": "Magento/theme/layout.xml",
                }
            ],
            "extension_attributes": {},
        }

    monkeypatch.setattr("services.apply.client.get_product", fake_get_product)

    diff, resolved, payload = build_product_payload(
        "SKU123",
        {"custom_layout_update_file": "Magento/theme/layout.xml"},
    )

    response, mismatches, _ = apply_product_update(
        "SKU123", diff, resolved
    )

    assert mismatches == {}
    assert response == {"status": "ok"}
    assert captured["endpoint"] == "products/SKU123"
    assert captured["payload"] == payload
