import sys
import unittest
from pathlib import Path
from unittest import mock

import requests

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from connectors.magento import MagentoClient


class MagentoClientTestCase(unittest.TestCase):
    def setUp(self):
        self.client = MagentoClient("https://example.com", "token")

    def test_iter_products_filters_attribute_set_and_qty_gt_zero(self):
        call_log = []

        def fake_get(path, params=None):
            call_log.append((path, params))
            if path == "/rest/V1/products":
                return (
                    {
                        "items": [
                            {
                                "sku": "A",
                                "attribute_set_id": 4,
                                "extension_attributes": {"stock_item": {"qty": 1}},
                            },
                            {
                                "sku": "B",
                                "attribute_set_id": 99,
                                "extension_attributes": {"stock_item": {"qty": 10}},
                            },
                        ],
                        "total_count": 2,
                    },
                    None,
                )
            raise AssertionError(f"Unexpected path: {path}")

        with mock.patch.object(self.client, "get", side_effect=fake_get):
            products = list(self.client.iter_products_qty_gt())

        self.assertEqual([p["sku"] for p in products], ["A"])
        self.assertEqual(len(call_log), 1)
        product_path, product_params = call_log[0]
        self.assertEqual(product_path, "/rest/V1/products")
        self.assertEqual(
            product_params["searchCriteria[filter_groups][0][filters][0][field]"],
            "attribute_set_id",
        )
        self.assertEqual(
            product_params["searchCriteria[filter_groups][0][filters][0][value]"],
            4,
        )
        self.assertEqual(
            product_params["searchCriteria[filter_groups][0][filters][0][condition_type]"],
            "eq",
        )

    def test_iter_products_falls_back_to_default_attribute_set(self):
        call_log = []

        def fake_get(path, params=None):
            call_log.append((path, params))
            if path == "/rest/V1/eav/attribute-sets/list":
                raise requests.HTTPError("boom")
            return (
                {
                    "items": [
                        {
                            "sku": "A",
                            "attribute_set_id": 4,
                            "extension_attributes": {"stock_item": {"qty": 5, "is_in_stock": True}},
                        }
                    ],
                    "total_count": 1,
                },
                None,
            )

        with mock.patch.object(self.client, "get", side_effect=fake_get):
            products = list(
                self.client.iter_products_qty_gt(
                    attribute_set_id=None, attribute_set_name="Custom"
                )
            )

        self.assertEqual([p["sku"] for p in products], ["A"])
        self.assertEqual(call_log[0][0], "/rest/V1/eav/attribute-sets/list")
        _, product_params = call_log[1]
        self.assertEqual(
            product_params["searchCriteria[filter_groups][0][filters][0][value]"],
            4,
        )

    def test_iter_products_fetches_legacy_stock_item_when_missing_qty(self):
        call_log = []

        def fake_get(path, params=None):
            call_log.append((path, params))
            if path == "/rest/V1/products":
                return (
                    {
                        "items": [
                            {
                                "sku": "A",
                                "attribute_set_id": 4,
                                "extension_attributes": {},
                            }
                        ],
                        "total_count": 1,
                    },
                    None,
                )
            if path == "/rest/V1/inventory/get-product-salable-quantity/A/1":
                return (0, None)
            if path == "/rest/V1/stockItems/A":
                return ({"qty": 2, "is_in_stock": True}, None)
            raise AssertionError(f"Unexpected path: {path}")

        with mock.patch.object(self.client, "get", side_effect=fake_get):
            products = list(self.client.iter_products_qty_gt())

        self.assertEqual([p["sku"] for p in products], ["A"])
        self.assertEqual(
            products[0]["extension_attributes"]["stock_item"]["qty"], 2
        )
        self.assertIn(("/rest/V1/stockItems/A", None), call_log)
        self.assertIn(
            ("/rest/V1/inventory/get-product-salable-quantity/A/1", None), call_log
        )

    def test_get_default_products_returns_items_payload_with_qty(self):
        sample_product = {
            "sku": "A",
            "extension_attributes": {"stock_item": {"qty": 7}},
        }

        with mock.patch.object(
            self.client,
            "iter_products_qty_gt",
            return_value=iter([sample_product]),
        ):
            payload = self.client.get_default_products()

        self.assertEqual(len(payload["items"]), 1)
        self.assertEqual(
            payload["items"][0]["extension_attributes"]["stock_item"]["qty"], 7
        )


if __name__ == "__main__":
    unittest.main()
