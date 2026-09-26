"""Profile-scoped configuration and runtime paths for MatchAtlas."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


SUPPORTED_PROFILES = ("kk", "sandra")


@dataclass(frozen=True)
class ProfileWorkspace:
    profile_id: str
    root: Path
    profile_dir: Path
    private_dir: Path
    state_dir: Path
    config: dict[str, Any]
    candidate_profile: dict[str, Any]

    @classmethod
    def load(cls, profile_id: str, root: str | Path | None = None) -> "ProfileWorkspace":
        normalized = (profile_id or "").strip().lower()
        if normalized not in SUPPORTED_PROFILES:
            raise ValueError(f"unsupported profile: {profile_id!r}")

        repo_root = Path(root or Path(__file__).resolve().parent).resolve()
        profile_dir = (repo_root / "profiles" / normalized).resolve()
        private_dir = (profile_dir / "private").resolve()
        state_dir = (profile_dir / "state").resolve()
        if profile_dir.parent != (repo_root / "profiles").resolve():
            raise ValueError("profile path escapes repository")

        config_path = private_dir / "config.json"
        candidate_path = private_dir / "candidate_profile.json"
        if not config_path.is_file() or not candidate_path.is_file():
            raise FileNotFoundError(
                f"profile {normalized!r} requires private/config.json and "
                "private/candidate_profile.json"
            )
        config = json.loads(config_path.read_text(encoding="utf-8-sig"))
        declared = str(config.get("profile_id") or "").strip().lower()
        if declared != normalized:
            raise ValueError(
                f"profile config mismatch: requested {normalized!r}, declared {declared!r}"
            )
        candidate_profile = json.loads(candidate_path.read_text(encoding="utf-8-sig"))
        state_dir.mkdir(parents=True, exist_ok=True)
        return cls(
            profile_id=normalized,
            root=repo_root,
            profile_dir=profile_dir,
            private_dir=private_dir,
            state_dir=state_dir,
            config=config,
            candidate_profile=candidate_profile,
        )

    def output_path(self, name: str) -> Path:
        candidate = (self.state_dir / name).resolve()
        if candidate.parent != self.state_dir:
            raise ValueError(f"invalid state filename: {name!r}")
        return candidate

    def private_path(self, name: str) -> Path:
        candidate = (self.private_dir / name).resolve()
        if candidate.parent != self.private_dir:
            raise ValueError(f"invalid private filename: {name!r}")
        return candidate
