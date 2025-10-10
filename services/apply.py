from connectors.magento import client


def apply_product_update(sku, attributes):
    payload = {
        "product": {
            "sku": sku,
            "custom_attributes": [
                {"attribute_code": k, "value": v} for k, v in attributes.items()
            ]
        }
    }
    return client.patch(f"products/{sku}", payload)
