from dotenv import load_dotenv

# Runs before any app.* submodule, regardless of import order - app/llm.py
# constructs its Anthropic client at import time, so .env must be loaded
# before that happens, not just whenever app.db happens to get imported.
load_dotenv()
