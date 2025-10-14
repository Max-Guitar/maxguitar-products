"""HTTP client helpers for interacting with Magento's REST API."""

from functools import lru_cache
from typing import Any, Dict, Iterable, List, Union
from urllib.parse import quote

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

    def get_product(self, sku: Union[int, str]) -> Dict[str, Any]:
        """Return a single product by SKU, ensuring the SKU is safe for URL usage."""

        encoded_sku = quote(str(sku), safe="")
        return self.get(f"products/{encoded_sku}")

    @lru_cache(maxsize=1)
    def get_attribute_sets(self):
        """Return a cached mapping of attribute set id -> name using robust endpoint discovery."""

        candidates = [
            ("products/attribute-sets/sets/list", {"searchCriteria[pageSize]": 200}),
            ("eav/attribute-sets/list", {"searchCriteria[pageSize]": 200}),
            ("products/attribute-sets/list", {"searchCriteria[pageSize]": 200}),
            ("products/attribute-sets/setsList", {"searchCriteria[current_page]": 1}),
        ]
        last_err = None

        for endpoint, params in candidates:
            try:
                data = self.get(endpoint, params=params)
            except Exception as exc:  # pragma: no cover - defensive against Magento variants
                last_err = exc
                continue

            items = data.get("items", data) if isinstance(data, dict) else data
            sets = {}
            for item in items or []:
                try:
                    set_id = int(item.get("attribute_set_id") or item.get("id"))
                except (TypeError, ValueError):
                    continue
                name = item.get("attribute_set_name") or item.get("name")
                if set_id and name:
                    sets[set_id] = name
            if sets:
                return sets

        raise RuntimeError("Failed to fetch attribute sets via all known endpoints") from last_err

    def get_attribute_groups(self, attribute_set_id: Union[int, str]) -> List[Dict[str, Any]]:
        """Return attribute groups for the given attribute set."""

        params = {
            "searchCriteria[filter_groups][0][filters][0][field]": "attribute_set_id",
            "searchCriteria[filter_groups][0][filters][0][value]": str(attribute_set_id),
            "searchCriteria[filter_groups][0][filters][0][condition_type]": "eq",
            "searchCriteria[pageSize]": 500,
        }
        data = self.get("products/attribute-sets/groups/list", params=params)
        items: Iterable[Dict[str, Any]]
        if isinstance(data, dict):
            items = data.get("items", []) or []
        else:
            items = data or []
        return list(items)

    def get_attributes_for_set(self, set_id: Union[int, str]) -> List[Dict[str, Any]]:
        """Return all attributes associated with a specific attribute set.

        Magento instances can expose this information via different endpoints. We
        attempt the modern attribute-set endpoint first and gracefully fall back
        to the legacy search-based endpoint if necessary.
        """

        try:
            data = self.get(f"products/attribute-sets/{set_id}/attributes")
        except Exception:  # pragma: no cover - compatibility with Magento variants
            params = {
                "searchCriteria[filter_groups][0][filters][0][field]": "attribute_set_id",
                "searchCriteria[filter_groups][0][filters][0][value]": str(set_id),
                "searchCriteria[pageSize]": 500,
            }
            data = self.get("products/attributes", params=params)
        items: Iterable[Dict[str, Any]]
        if isinstance(data, dict):
            items = data.get("items", []) or []
        else:
            items = data or []
        return list(items)

    def get_attributes_for_group(
        self, attribute_set_id: Union[int, str], group_id: Union[int, str]
    ) -> List[Dict[str, Any]]:
        """Return attributes belonging to a specific attribute group.

        Some Magento endpoints only need the group identifier in the path, but we
        keep the attribute set id in the signature for clarity and potential
        future validation.
        """

        data = self.get(f"products/attribute-sets/groups/{group_id}/attributes")
        items: Iterable[Dict[str, Any]]
        if isinstance(data, dict):
            items = data.get("items", []) or []
        else:
            items = data or []
        return list(items)

    def get_attribute(self, attribute_code: Union[int, str]) -> Dict[str, Any]:
        """Return metadata for a single attribute."""

        encoded_code = quote(str(attribute_code), safe="")
        return self.get(f"products/attributes/{encoded_code}")

    def get_attribute_options(self, attribute_code: Union[int, str]) -> List[Dict[str, Any]]:
        """Return dropdown/options metadata for a given attribute."""

        encoded_code = quote(str(attribute_code), safe="")
        data = self.get(f"products/attributes/{encoded_code}/options")
        if isinstance(data, dict):
            return list(data.get("items", []) or [])
        return list(data or [])


client = MagentoClient()
