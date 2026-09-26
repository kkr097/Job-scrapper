# Profile workspaces

Tracked files are safe examples only. Each profile needs ignored `private/` and
`state/` directories. `ProfileWorkspace` rejects unknown profiles and a
configuration whose declared `profile_id` does not match its directory.

- `private/config.json`, `private/candidate_profile.json`, `private/.env`, and
  résumé/evidence files never belong in Git.
- `state/` contains CSV queues, SQLite, logs, archives, and locks.
- All mutating commands require `--profile kk` or `--profile sandra`.
