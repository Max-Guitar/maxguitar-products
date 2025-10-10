# 🎸 Magento Product Enricher

A Streamlit-based internal tool to enrich Magento products with AI-generated specs and multilingual descriptions.

### Features
- Connects directly to Magento REST API
- Lists products with *default* attribute set
- Generates missing specs and descriptions via OpenAI
- Normalizes values to Magento options
- Allows review before writing back

### Usage
```bash
pip install -r requirements.txt
cp .env.sample .env
# fill credentials
streamlit run app/streamlit_app.py
```
