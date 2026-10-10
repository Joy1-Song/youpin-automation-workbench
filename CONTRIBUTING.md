# Contributing

1. Start with `python demo.py`; no credentials are needed.
2. Install `requirements-dev.txt` and run `python -m pytest -q`.
3. Keep strategy calculations separate from API/ADB side effects. Add synthetic regression cases for behavioral changes.
4. Do not commit runtime state, personal order exports, credentials or screenshots of an authenticated account.
5. Run `python scripts/check_publication.py` before sharing commits.

Tests intentionally block network connections and subprocesses. Never remove these guards to make a failing test reach a real account. CI should not receive trading or SMTP credentials.

This repository is a portfolio project with evolving interfaces. Discuss broader storage or UI rewrites separately from focused fixes. No license has been selected yet; public visibility alone is not a grant of an open-source license.
