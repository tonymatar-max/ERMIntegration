# Run the ERM Project Ledger web app locally / on a Windows server.
#
# First-time setup:
#   python -m venv .venv
#   .venv\Scripts\Activate.ps1
#   pip install -r requirements.txt
#
# Then just run this script. No manual secret-key setup needed — the app
# generates and persists its own key on first run (data/secret.key). Set
# $env:ERM_SECRET_KEY yourself only if you specifically want to manage the
# key externally (e.g. a secrets manager, or sharing one key across
# multiple instances of this app).

python -m uvicorn app.main:app --host 0.0.0.0 --port 9020
