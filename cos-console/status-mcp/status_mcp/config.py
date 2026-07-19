"""Project registry — maps a logical project key to the concrete host signals.

Everything here is a *pointer* to where a signal lives; no secrets. Paths use
``~`` expansion. Add a project by adding an entry; nothing else needs to change.

Overridable via env (see .env.example) so this dry-runs on another host without
editing code: STATUS_MCP_DEPLOY_INDEX, STATUS_MCP_<KEY>_REPO_PATH, etc.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


@dataclass
class ProjectConfig:
    key: str
    # GitHub
    gh_repo: Optional[str] = None          # owner/repo for issues
    gh_project_number: Optional[int] = None  # Projects (v2) board number
    # Local checkout used for tests + git-mined decisions
    repo_path: Optional[str] = None
    # Maven artifact roots (relative to repo_path unless absolute)
    surefire_dir: str = "target/surefire-reports"
    jacoco_csv: str = "target/site/jacoco/jacoco.csv"
    jacoco_xml: str = "target/site/jacoco/jacoco.xml"
    # Deploy: which target name to match in the deploy-log INDEX.md
    deploy_target: Optional[str] = None
    deploy_index: Optional[str] = None      # path to deploy_logs/INDEX.md
    # Visual review / demos
    demos_dir: Optional[str] = None
    # Extra dirs to scan for close-work / handoff briefs (decisions)
    brief_dirs: list = field(default_factory=list)

    def resolved_repo_path(self) -> Optional[Path]:
        return _expand(self.repo_path)


def _expand(p: Optional[str]) -> Optional[Path]:
    if not p:
        return None
    return Path(os.path.expanduser(p)).resolve() if os.path.exists(os.path.expanduser(p)) \
        else Path(os.path.expanduser(p))


# --- Registry ---------------------------------------------------------------
# dstrader is the first real target (has deploys + visual-review history).
_REGISTRY = {
    "dstrader": ProjectConfig(
        key="dstrader",
        gh_repo="jdtogni78/dstrader",
        gh_project_number=1,  # "Trading & Fund" board
        repo_path="~/dev/dstrader",
        deploy_target="dstrader-docker",
        deploy_index="~/dev/dstrader-docker/local/deploy_logs/INDEX.md",
        demos_dir="~/dev/dstrader/demos",
        brief_dirs=["~/.ai-harness/handoffs"],
    ),
    # FamilyFund shares board 1; deploys under the 'familyfund' target.
    "familyfund": ProjectConfig(
        key="familyfund",
        gh_repo="jdtogni78/FamilyFund",
        gh_project_number=1,
        repo_path="~/dev/FamilyFund",
        deploy_target="familyfund",
        deploy_index="~/dev/dstrader-docker/local/deploy_logs/INDEX.md",
        demos_dir="~/dev/FamilyFund/demos",
        brief_dirs=["~/.ai-harness/handoffs"],
    ),
}


def _apply_env_overrides(cfg: ProjectConfig) -> ProjectConfig:
    """Let env vars override paths so this runs on a different host untouched."""
    idx = os.environ.get("STATUS_MCP_DEPLOY_INDEX")
    if idx:
        cfg.deploy_index = idx
    key = cfg.key.upper()
    rp = os.environ.get(f"STATUS_MCP_{key}_REPO_PATH")
    if rp:
        cfg.repo_path = rp
    dm = os.environ.get(f"STATUS_MCP_{key}_DEMOS_DIR")
    if dm:
        cfg.demos_dir = dm
    return cfg


def get_project(key: str) -> ProjectConfig:
    k = (key or "").strip().lower()
    if k not in _REGISTRY:
        raise KeyError(
            f"Unknown project '{key}'. Known: {', '.join(sorted(_REGISTRY))}. "
            f"Add one in status_mcp/config.py."
        )
    return _apply_env_overrides(_REGISTRY[k])


def list_projects() -> list[str]:
    return sorted(_REGISTRY)
