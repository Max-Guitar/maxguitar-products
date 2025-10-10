import httpx
from tenacity import retry, stop_after_attempt, wait_exponential
from config import settings

class MagentoClient:
    def __init__(self):
        self.base_url = settings.MAGENTO_BASE_URL.rstrip("/")
        self.token = settings.MAGENTO_ADMIN_TOKEN
        self.headers = {"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"}

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=10))
    def get(self, endpoint, params=None):
        url = f"{self.base_url}/rest/V1/{endpoint.lstrip('/')}"
        r = httpx.get(url, headers=self.headers, params=params, timeout=30)
        r.raise_for_status()
        return r.json()

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=10))
    def patch(self, endpoint, payload):
        url = f"{self.base_url}/rest/V1/{endpoint.lstrip('/')}"
        r = httpx.put(url, headers=self.headers, json=payload, timeout=30)
        r.raise_for_status()
        return r.json()

    def get_default_products(self):
        search = {
            "searchCriteria[filter_groups][0][filters][0][field]": "attribute_set_id",
            "searchCriteria[filter_groups][0][filters][0][value]": "4",  # Default set id
            "searchCriteria[filter_groups][0][filters][0][condition_type]": "eq",
        }
        return self.get("products", params=search)

client = MagentoClient()
