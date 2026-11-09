"""Minimal OpenRouter helpers — fetch the available model list (for the App
Settings dropdown) and a connection test. The models endpoint is public (no
key needed); chat completions use the saved key.
"""

import requests

from .logging_config import logger

BASE_URL = "https://openrouter.ai/api/v1"

# Shown when the live model list can't be fetched (offline/blocked) so the
# dropdown is never empty. The saved model is always added on top of this.
def _base(base_url: str = "") -> str:
    """The chat-completions root: a custom OpenAI-compatible endpoint if one is
    configured, otherwise OpenRouter."""
    return (base_url or "").strip().rstrip("/") or BASE_URL


FALLBACK_MODELS = [
    "anthropic/claude-3.5-sonnet",
    "anthropic/claude-3.5-haiku",
    "openai/gpt-4o",
    "openai/gpt-4o-mini",
    "google/gemini-flash-1.5",
    "meta-llama/llama-3.1-70b-instruct",
]


def fetch_models(timeout: int = 8, base_url: str = "", api_key: str = "") -> list:
    """Sorted list of OpenRouter model ids. Falls back to FALLBACK_MODELS if the
    request fails. The list endpoint is public, so no API key is required."""
    try:
        resp = requests.get(f"{_base(base_url)}/models", timeout=timeout,
                            headers={"Authorization": f"Bearer {api_key}"} if api_key else {})
        resp.raise_for_status()
        ids = sorted({m.get("id") for m in resp.json().get("data", []) if m.get("id")})
        return ids or FALLBACK_MODELS
    except Exception as e:
        logger.warning("Could not fetch OpenRouter models (%s) — using fallback list.", e)
        return FALLBACK_MODELS


class OpenRouterError(Exception):
    pass


def _one_completion(api_key: str, model: str, messages: list, timeout: int, max_tokens: int, base_url: str = "") -> str:
    """Call a single model; return its text, or "" if it produced none. Raises
    OpenRouterError only for transport/HTTP errors (so the caller can fall back)."""
    try:
        resp = requests.post(
            f"{_base(base_url)}/chat/completions",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json",
                     "HTTP-Referer": "https://erm.seidor", "X-Title": "ERM Project Ledger"},
            json={"model": model, "messages": messages, "max_tokens": max_tokens, "temperature": 0.3},
            timeout=timeout,
        )
    except Exception as e:
        raise OpenRouterError(f"could not reach OpenRouter: {e}")
    if resp.status_code != 200:
        detail = ""
        try:
            detail = (resp.json().get("error") or {}).get("message") or ""
        except Exception:
            detail = resp.text[:160]
        raise OpenRouterError(f"HTTP {resp.status_code}: {detail}")
    try:
        msg = (resp.json().get("choices") or [{}])[0].get("message") or {}
    except (ValueError, IndexError, AttributeError, TypeError):
        return ""
    content = msg.get("content")
    if content is None:
        content = msg.get("reasoning")  # some reasoning-only models
    return str(content).strip() if content else ""


def chat(api_key: str, models, messages: list, timeout: int = 60, max_tokens: int = 900, base_url: str = "") -> dict:
    """A chat completion with model fallback. `models` is a list (primary first,
    then fallbacks, max 3) or a single string. Each model is tried in order and
    one that errors OR returns no text (reranker/embedding/empty) is skipped, so
    a bad primary doesn't break the feature. Returns {text, model} of the model
    that actually answered; raises OpenRouterError only if all fail."""
    if not api_key:
        raise OpenRouterError("No OpenRouter API key configured (App Settings → OpenRouter).")
    model_list = [m for m in ([models] if isinstance(models, str) else list(models or [])) if m][:3]
    if not model_list:
        raise OpenRouterError("No OpenRouter model configured (App Settings → OpenRouter).")

    errors = []
    for model in model_list:
        try:
            text = _one_completion(api_key, model, messages, timeout, max_tokens, base_url)
        except OpenRouterError as e:
            errors.append(f"{model}: {e}")
            continue
        if text:
            return {"text": text, "model": model}
        errors.append(f"{model}: returned no text (not a chat model?)")

    if len(model_list) == 1:
        raise OpenRouterError(
            f"{model_list[0]} {errors[0].split(': ', 1)[-1]}. "
            "Try a text/chat model (e.g. openai/gpt-4o-mini) in App Settings → OpenRouter."
        )
    raise OpenRouterError("All configured models failed — " + "; ".join(errors))


def test_connection(api_key: str, timeout: int = 10, base_url: str = "") -> tuple:
    """(ok, message) — verifies the API key by calling the authenticated
    /key endpoint (OpenRouter) or /models (a custom endpoint). Does not spend
    tokens."""
    if not api_key:
        return False, "No API key set."
    if (base_url or "").strip():
        try:
            resp = requests.get(f"{_base(base_url)}/models", headers={"Authorization": f"Bearer {api_key}"}, timeout=timeout)
            if resp.status_code == 200:
                n = len(resp.json().get("data", []))
                return True, f"Endpoint reachable and key accepted ({n} models available)."
            if resp.status_code in (401, 403):
                return False, "The endpoint rejected the API key (unauthorized)."
            return False, f"The endpoint returned HTTP {resp.status_code}."
        except Exception as e:
            return False, f"Could not reach {_base(base_url)}: {e}"
    try:
        resp = requests.get(f"{BASE_URL}/key", headers={"Authorization": f"Bearer {api_key}"}, timeout=timeout)
        if resp.status_code == 200:
            return True, "OpenRouter key is valid."
        if resp.status_code in (401, 403):
            return False, "OpenRouter rejected the API key (unauthorized)."
        return False, f"OpenRouter returned HTTP {resp.status_code}."
    except Exception as e:
        return False, f"Could not reach OpenRouter: {e}"
