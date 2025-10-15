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
    payload = build_product_payload(
        "SKU123",
        {"custom_layout_update_file": value},
    )

    assert payload == {
        "product": {
            "sku": "SKU123",
            "custom_attributes": [],
            "extension_attributes": {},
        }
    }


def test_custom_layout_update_file_passes_through_real_path():
    payload = build_product_payload(
        "SKU123",
        {"custom_layout_update_file": "Magento/theme/layout.xml"},
    )

    assert payload == {
        "product": {
            "sku": "SKU123",
            "custom_attributes": [
                {
                    "attribute_code": "custom_layout_update_file",
                    "value": "Magento/theme/layout.xml",
                }
            ],
            "extension_attributes": {},
        }
    }


def test_apply_product_update_regenerates_payload(monkeypatch: pytest.MonkeyPatch):
    captured = _capture_patch(monkeypatch)

    # Provide a pre-built payload that still includes the sentinel value – the
    # helper should regenerate it to remove the attribute entirely.
    prebuilt_payload = {
        "product": {
            "sku": "SKU123",
            "custom_attributes": [
                {
                    "attribute_code": "custom_layout_update_file",
                    "value": "__no_update__",
                }
            ],
        }
    }

    response = apply_product_update(
        "SKU123",
        {"custom_layout_update_file": "__no_update__"},
        metadata=None,
        payload=prebuilt_payload,
    )

    assert response == {"status": "ok"}
    assert captured["endpoint"] == "products/SKU123"
    assert captured["payload"] == {
        "product": {
            "sku": "SKU123",
            "custom_attributes": [],
            "extension_attributes": {},
        }
    }
