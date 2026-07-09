#!/usr/bin/env python3
"""Initialize SQLite settings: OpenAI as default LLM provider."""

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from backend.db import ensure_db
from backend.settings_manager import save_llm_config, apply_llm_config_to_default, get_llm_config

ensure_db()
save_llm_config(
    provider="openai",
    deep_model="gpt-4o",
    quick_model="gpt-4o-mini",
)
apply_llm_config_to_default()
print("LLM config:", get_llm_config())
