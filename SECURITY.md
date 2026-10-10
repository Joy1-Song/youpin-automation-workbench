# Security

- Keep `.env`, `config.local.json`, `state/` and `logs/` private, including their backups.
- Do not paste account sessions, cookies, Steam credentials, SMTP passwords or personal transaction data into issues or pull requests.
- If a credential has already been disclosed, revoke it at its provider. Removing it from the working tree does not revoke it or erase Git history.
- Plugins execute trusted local Python code. They are not sandboxed.
- The command queue is local, single-writer state, not a distributed transaction log. Stop and reconcile remote orders after uncertain execution outcomes.
- The public configuration disables automatic submission. Dry-run may still perform online reads; use `demo.py` for a fully offline walkthrough.
- The publication checker is a defense in depth, not a guarantee that every secret format can be detected. Review staged changes manually.

Report a vulnerability without including credentials or account data. If GitHub private vulnerability reporting is available for this repository, prefer it over a public issue.
