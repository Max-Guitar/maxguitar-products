"""Utilities for persisting enriched product attributes back to Magento."""

from connectors.magento import client


def apply_product_update(sku, attributes):
    """Send a Magento product update request for the provided SKU."""
    payload = {
        "product": {
            "sku": sku,
            "custom_attributes": [
                {"attribute_code": k, "value": v} for k, v in attributes.items()
            ]
        }
    }
    return client.patch(f"products/{sku}", payload)
