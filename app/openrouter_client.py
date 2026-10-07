"""Minimal OpenRouter helpers — fetch the available model list (for the App
Settings dropdown) and a connection test. The models endpoint is public (no
key needed); chat completions use the saved key.
"""

import requests

from .logging_config import logger

BASE_URL = "https://openrouter.ai/api/v1"

# Shown when the live model list can't be fetched (offline/blocked) so the
# dropdown is never empty. The saved model is always added on top of this.
FALLBACK_MODELS = [
    "anthropic/claude-3.5-sonnet",
    "anthropic/claude-3.5-haiku",
    "openai/gpt-4o",
    "openai/gpt-4o-mini",
    "google/gemini-flash-1.5",
    "meta-llama/llama-3.1-70b-instruct",
]


def fetch_models(timeout: int = 8) -> list:
    """Sorted list of OpenRouter model ids. Falls back to FALLBACK_MODELS if the
    request fails. The list endpoint is public, so no API key is required."""
    try:
        resp = requests.get(f"{BASE_URL}/models", timeout=timeout)
        resp.raise_for_status()
        ids = sorted({m.get("id") for m in resp.json().get("data", []) if m.get("id")})
        return ids or FALLBACK_MODELS
    except Exception as e:
        logger.warning("Could not fetch OpenRouter models (%s) — using fallback list.", e)
        return FALLBACK_MODELS


class OpenRouterError(Exception):
    pass


def chat(api_key: str, model: str, messages: list, timeout: int = 60, max_tokens: int = 900) -> str:
    """One chat completion via OpenRouter; returns the assistant text. Raises
    OpenRouterError with a user-facing message on failure."""
    if not api_key:
        raise OpenRouterError("No OpenRouter API key configured (App Settings → OpenRouter).")
    if not model:
        raise OpenRouterError("No OpenRouter model selected (App Settings → OpenRouter).")
    try:
        resp = requests.post(
            f"{BASE_URL}/chat/completions",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json",
                     "HTTP-Referer": "https://erm.seidor", "X-Title": "ERM Project Ledger"},
            json={"model": model, "messages": messages, "max_tokens": max_tokens, "temperature": 0.3},
            timeout=timeout,
        )
    except Exception as e:
        raise OpenRouterError(f"Could not reach OpenRouter: {e}")
    if resp.status_code != 200:
        detail = ""
        try:
            detail = (resp.json().get("error") or {}).get("message") or ""
        except Exception:
            detail = resp.text[:200]
        raise OpenRouterError(f"OpenRouter error (HTTP {resp.status_code}): {detail}")
    try:
        return resp.json()["choices"][0]["message"]["content"].strip()
    except (KeyError, IndexError, ValueError):
        raise OpenRouterError("OpenRouter returned an unexpected response shape.")


def test_connection(api_key: str, timeout: int = 10) -> tuple:
    """(ok, message) — verifies the API key by calling the authenticated
    /key endpoint. Does not spend tokens."""
    if not api_key:
        return False, "No API key set."
    try:
        resp = requests.get(f"{BASE_URL}/key", headers={"Authorization": f"Bearer {api_key}"}, timeout=timeout)
        if resp.status_code == 200:
            return True, "OpenRouter key is valid."
        if resp.status_code in (401, 403):
            return False, "OpenRouter rejected the API key (unauthorized)."
        return False, f"OpenRouter returned HTTP {resp.status_code}."
    except Exception as e:
        return False, f"Could not reach OpenRouter: {e}"
