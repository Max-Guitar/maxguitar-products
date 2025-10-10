# 🎸 Magento Product Enricher

A Streamlit-based internal tool for merchandising teams to enrich Magento products with AI-generated specs and descriptions. The app sits between the Magento REST API and OpenAI to suggest structured attribute updates that can be reviewed before publishing.

## What the app does
- **Discover catalog gaps.** Fetches products assigned to the default Magento attribute set so merchants can focus on incomplete listings.
- **Generate missing specs.** Sends product names (plus an optional user hint) to OpenAI to infer bridge type, string count, finish, pickups, handedness, and other guitar characteristics.
- **Normalize to Magento options.** Uses fuzzy matching against `data/attributes_map.yaml` so model output aligns with allowed attribute values.
- **Review and apply.** Surfaces generated data in the Streamlit UI, allowing merchants to confirm and then push the updates back to Magento via the REST API.

## Architecture at a glance
| Layer | Purpose | Key files |
| --- | --- | --- |
| Configuration | Loads Magento and OpenAI credentials from environment variables | `config.py`, `.env.sample` |
| Connectors | Resilient HTTP client for Magento REST calls | `connectors/magento.py` |
| Services | LLM prompting, value normalization, and applying Magento updates | `services/llm_extract.py`, `services/normalize.py`, `services/apply.py` |
| UI | Streamlit dashboard for loading, enriching, and writing products | `app/streamlit_app.py` |
| Data | Attribute normalization mappings | `data/attributes_map.yaml` |

## Running locally
```bash
pip install -r requirements.txt
cp .env.sample .env
# Fill in Magento and OpenAI credentials
streamlit run app/streamlit_app.py
```

## Development notes
- The LLM response format is constrained to JSON for easy parsing.
- `rapidfuzz` normalizes freeform specs into Magento-compliant values; extend `data/attributes_map.yaml` as new options appear.
- The Magento client retries transient failures with exponential backoff to handle flaky network conditions.
