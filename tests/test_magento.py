import unittest
from unittest import mock

import requests

from connectors.magento import MagentoClient


class MagentoClientTestCase(unittest.TestCase):
    def setUp(self):
        self.client = MagentoClient("https://example.com", "token")

    def test_iter_products_filters_attribute_set(self):
        responses = [
            ({"items": [{"attribute_set_name": "Default", "attribute_set_id": 12}]}, None),
            (
                {
                    "items": [
                        {
                            "sku": "A",
                            "attribute_set_id": 12,
                            "extension_attributes": {"stock_item": {"qty": 3, "is_in_stock": True}},
                        },
                        {
                            "sku": "B",
                            "attribute_set_id": 99,
                            "extension_attributes": {"stock_item": {"qty": 3, "is_in_stock": True}},
                        },
                    ],
                    "total_count": 2,
                },
                None,
            ),
        ]

        call_log = []

        def fake_get(path, params=None):
            call_log.append((path, params))
            data, response = responses.pop(0)
            return data, response

        with mock.patch.object(self.client, "get", side_effect=fake_get):
            products = list(self.client.iter_products_qty_gt())

        self.assertEqual([p["sku"] for p in products], ["A"])
        self.assertEqual(call_log[0][0], "/rest/V1/eav/attribute-sets/list")

        product_path, product_params = call_log[1]
        self.assertEqual(product_path, "/rest/V1/products")
        self.assertEqual(
            product_params["searchCriteria[filter_groups][0][filters][0][field]"],
            "attribute_set_id",
        )
        self.assertEqual(
            product_params["searchCriteria[filter_groups][0][filters][0][value]"],
            12,
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
                            "attribute_set_id": 12,
                            "extension_attributes": {"stock_item": {"qty": 5, "is_in_stock": True}},
                        }
                    ],
                    "total_count": 1,
                },
                None,
            )

        with mock.patch.object(self.client, "get", side_effect=fake_get):
            products = list(self.client.iter_products_qty_gt())

        self.assertEqual([p["sku"] for p in products], ["A"])
        _, product_params = call_log[1]
        self.assertEqual(
            product_params["searchCriteria[filter_groups][0][filters][0][value]"],
            12,
        )

    def test_get_default_products_returns_items_payload(self):
        sample_products = [
            {
                "sku": "A",
                "extension_attributes": {"stock_item": {"qty": 3, "is_in_stock": True}},
            }
        ]

        with mock.patch.object(
            self.client,
            "iter_products_qty_gt",
            return_value=iter(sample_products),
        ):
            payload = self.client.get_default_products()

        self.assertEqual(payload, {"items": sample_products})


if __name__ == "__main__":
    unittest.main()
