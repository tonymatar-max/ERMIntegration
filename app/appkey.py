"""App-level secret key — encrypts stored Redmine API keys at rest and
signs session cookies.

No manual setup step: on first run, a key is generated and saved to
data/secret.key (0600 permissions where the OS supports it) and reused on
every subsequent start. This is NOT a per-user Redmine API key — those are
supplied by each user in the Settings page.

An ERM_SECRET_KEY environment variable, if set, always takes precedence
over the persisted file — useful for a deployment that wants to manage the
key itself (e.g. a secrets manager, or sharing one key across multiple
instances of this app behind a load balancer). Most single-instance
deployments don't need to set anything.
"""

import os
import stat

from cryptography.fernet import Fernet

DEFAULT_KEY_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "secret.key")
KEY_PATH = os.environ.get("ERM_KEY_PATH", DEFAULT_KEY_PATH)

_cached_key = None


def get_key() -> str:
    """Returns the app secret as a string, generating and persisting one
    on first use if neither ERM_SECRET_KEY nor a saved key file exists."""
    global _cached_key
    if _cached_key:
        return _cached_key

    env_key = os.environ.get("ERM_SECRET_KEY")
    if env_key:
        _cached_key = env_key
        return _cached_key

    if os.path.exists(KEY_PATH):
        with open(KEY_PATH, "r", encoding="ascii") as f:
            _cached_key = f.read().strip()
        return _cached_key

    os.makedirs(os.path.dirname(KEY_PATH), exist_ok=True)
    new_key = Fernet.generate_key().decode("ascii")
    with open(KEY_PATH, "w", encoding="ascii") as f:
        f.write(new_key)
    try:
        os.chmod(KEY_PATH, stat.S_IRUSR | stat.S_IWUSR)  # 0600 — no-op on Windows, harmless
    except OSError:
        pass
    _cached_key = new_key
    return _cached_key
