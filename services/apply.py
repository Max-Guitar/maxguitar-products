"""Utilities for persisting enriched product attributes back to Magento."""

from connectors.magento import client


def apply_product_update(sku, attributes):
    """Send a Magento product update request for the provided SKU without retries."""

    payload = {
        "product": {
            "sku": sku,
            "custom_attributes": [
                {"attribute_code": k, "value": v} for k, v in attributes.items()
            ],
        }
    }

    patch_func = getattr(client.patch, "__wrapped__", None)
    if callable(patch_func):
        return patch_func(client, f"products/{sku}", payload)

    return client.patch(f"products/{sku}", payload)
