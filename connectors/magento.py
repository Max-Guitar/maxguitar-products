"""HTTP client helpers for interacting with Magento's REST API."""

from functools import lru_cache

import httpx
from tenacity import retry, stop_after_attempt, wait_exponential

from config import settings


class MagentoClient:
    """Thin wrapper around Magento REST endpoints used by the Streamlit app."""

    def __init__(self):
        self.base_url = settings.MAGENTO_BASE_URL.rstrip("/")
        self.token = settings.MAGENTO_ADMIN_TOKEN
        self.headers = {"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"}

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=10))
    def get(self, endpoint, params=None):
        """Perform a GET request against a Magento REST endpoint."""
        url = f"{self.base_url}/rest/V1/{endpoint.lstrip('/')}"
        r = httpx.get(url, headers=self.headers, params=params, timeout=30)
        r.raise_for_status()
        return r.json()

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=10))
    def patch(self, endpoint, payload):
        """Perform an update (HTTP PUT) request against a Magento REST endpoint."""
        url = f"{self.base_url}/rest/V1/{endpoint.lstrip('/')}"
        r = httpx.put(url, headers=self.headers, json=payload, timeout=30)
        r.raise_for_status()
        return r.json()

    def get_default_products(self):
        """Return products that belong to the default attribute set (id = 4)."""
        search = {
            "searchCriteria[filter_groups][0][filters][0][field]": "attribute_set_id",
            "searchCriteria[filter_groups][0][filters][0][value]": "4",  # Default set id
            "searchCriteria[filter_groups][0][filters][0][condition_type]": "eq",
        }
        return self.get("products", params=search)

    @lru_cache(maxsize=1)
    def get_attribute_sets(self):
        """Return a cached mapping of attribute set id -> name."""
        data = self.get("products/attribute-sets/setsList", params={"searchCriteria[current_page]": 1})
        sets = {int(item["attribute_set_id"]): item["attribute_set_name"] for item in data.get("items", [])}
        return sets

    def get_attributes_for_set(self, set_id):
        """Return all attributes associated with a specific attribute set."""
        params = {
            "searchCriteria[filter_groups][0][filters][0][field]": "attribute_set_id",
            "searchCriteria[filter_groups][0][filters][0][value]": set_id,
        }
        data = self.get("products/attributes", params=params)
        return data.get("items", [])


client = MagentoClient()
