"""Env-var tunables -> frozen dataclasses. Pure: ``from_env`` reads a mapping
(``os.environ`` by default) and never touches the filesystem or clock, so tests
construct configs from plain dicts.

Defaults are the same machine-specific absolute paths the zsh scripts baked in,
so the external contract (log paths, ports, plist labels) is unchanged.
"""
from __future__ import annotations

import os
import socket
from dataclasses import dataclass
from pathlib import Path
from typing import FrozenSet, Mapping, Optional, Tuple

from .discovery import nickname_from_hostname, normalize_host

# Canonical install location (these run as a login service; paths are absolute).
# Derived per-user from $HOME so the same code works on every account
# (mini=user, note=user2, ...). REMOTE_CONTROL_DEV / REMOTE_CONTROL_LOGDIR
# still override at runtime; tests inject `from_env({...})` directly.
DEV = str(Path.home() / "dev")
REPO = f"{DEV}/ai-harness"
LOGDIR = f"{REPO}/logs"
CLAUDE_BIN = str(Path.home() / ".local/bin/claude")
# Per-user, per-host allowlist (the supervisor's only knob for which dirs
# get servers). NOT in the repo: the file names private app dirs, so it
# lives under ~/.ai-harness/ (chmod 600). The installer seeds an empty
# template if missing. REMOTE_CONTROL_ACTIVE_FILE overrides this default.
ACTIVE_FILE = str(Path.home() / ".ai-harness" / "active-dirs.txt")


def _truthy(value: str) -> bool:
    return value.strip().lower() not in ("0", "false", "no", "")


def host_nickname(env: Optional[Mapping[str, str]] = None) -> str:
    """This machine's short nickname: explicit ``REMOTE_CONTROL_HOST`` if set,
    else derived from the hostname (``*macmini*`` -> ``mini``; see
    discovery.NICKNAME_RULES). Shared by every config that is host-aware, by
    the session-title host suffix, and by the supervisor's server-name prefix
    (``<host>-<basename>``). Set ``REMOTE_CONTROL_HOST`` in your plist to
    assign a per-machine personal nickname (the ``NICKNAME_RULES`` table is
    public, so personal nicknames belong in the override, not the defaults)."""
    env = os.environ if env is None else env
    explicit = env.get("REMOTE_CONTROL_HOST", "").strip()
    return normalize_host(explicit) if explicit else nickname_from_hostname(socket.gethostname())


@dataclass(frozen=True)
class SupervisorConfig:
    dev: Path
    claude_bin: Path
    logdir: Path
    active_file: Path
    tick_secs: int
    idle_recycle_secs: int
    grace_secs: int
    # This machine's nickname, used to match host-scoped active-dirs entries
    # (``basename@nick``). Explicit via REMOTE_CONTROL_HOST, else derived from
    # the hostname. See discovery.host_allows.
    host: str
    # Hysteresis: a running ``<host>-*`` server flagged as "not in allowlist"
    # must stay flagged across this many consecutive ticks before we actually
    # SIGTERM it. Defends against the launchd-only oscillation in issue #8,
    # where a single tick's ``discover()`` occasionally returns a partial
    # ``wanted`` set even though the allowlist + filesystem haven't changed --
    # the next tick recovers, but the old "kill on first miss" rule had already
    # killed (and forced a respawn of) every server the partial set omitted.
    # Default 2 (one extra tick = ~30s extra latency before a legit removal is
    # reaped, which is the price of the race fix); set to 1 to restore the old
    # behaviour for debugging.
    deactivate_min_strikes: int
    # Per-host state dir holding the rehydration ledger + one-off checkpoints
    # (see :mod:`remote_control.rehydrate`). Sibling of active-dirs.txt so the
    # entire ai-harness host-local state is under one tree.
    state_dir: Path
    # Rehydration cutoff: orphan transcripts older than this are NOT yanked
    # back -- a day-old thread reads as "done", not "interrupted". Hours,
    # matching the spec's env name (RESUME_TTL_HOURS).
    resume_ttl_hours: int

    @classmethod
    def from_env(cls, env: Optional[Mapping[str, str]] = None) -> "SupervisorConfig":
        env = os.environ if env is None else env
        # gethostname() is only consulted when no explicit nickname is set, so
        # tests stay deterministic by passing REMOTE_CONTROL_HOST.
        host = host_nickname(env)
        return cls(
            dev=Path(env.get("REMOTE_CONTROL_DEV", DEV)),
            claude_bin=Path(env.get("REMOTE_CONTROL_CLAUDE_BIN", CLAUDE_BIN)),
            logdir=Path(env.get("REMOTE_CONTROL_LOGDIR", LOGDIR)),
            active_file=Path(env.get("REMOTE_CONTROL_ACTIVE_FILE", ACTIVE_FILE)),
            tick_secs=int(env.get("TICK_SECS", "30")),                    # loop period
            idle_recycle_secs=int(env.get("IDLE_RECYCLE_SECS", "43200")),  # 12h
            grace_secs=int(env.get("GRACE_SECS", "10")),                 # TERM->KILL wait
            host=host,
            deactivate_min_strikes=max(1, int(env.get("DEACTIVATE_MIN_STRIKES", "2"))),
            state_dir=Path(env.get(
                "REMOTE_CONTROL_STATE_DIR",
                str(Path(env.get("HOME", str(Path.home()))) / ".ai-harness"))),
            resume_ttl_hours=int(env.get("RESUME_TTL_HOURS", "24")),
        )

    @property
    def manager_log(self) -> Path:
        return self.logdir / "manager.log"

    @property
    def oneoffs_dir(self) -> Path:
        return self.state_dir / "oneoffs"

    @property
    def rehydrated_dir(self) -> Path:
        return self.state_dir / "rehydrated"


@dataclass(frozen=True)
class UsageLimitConfig:
    home: Path
    logdir: Path
    api_base: str
    keychain_service: str
    detect_interval: int
    resume_interval: int
    http_timeout: int
    gc_age_secs: int
    # Repeated re-limits on resume -> wait this long before retrying, capped at
    # the last entry so a persistent monthly limit just retries every 30 min.
    backoffs_secs: Tuple[int, ...]
    # 0 = unlimited attempts (a monthly limit may need many until it resets).
    max_attempts: int
    resume_message: str
    resume_verify_secs: int
    dry_run: bool
    skip_session_ids: FrozenSet[str]

    @property
    def state_file(self) -> Path:
        return self.logdir / "paused-sessions.json"

    @property
    def log_file(self) -> Path:
        return self.logdir / "usage-limit-monitor.log"

    @property
    def lock_file(self) -> Path:
        return self.logdir / "usage-limit-monitor.lock"

    @classmethod
    def from_env(cls, env: Optional[Mapping[str, str]] = None) -> "UsageLimitConfig":
        env = os.environ if env is None else env
        skip = frozenset(
            s.strip() for s in env.get("USAGE_LIMIT_SKIP_SIDS", "").split(",") if s.strip()
        )
        return cls(
            home=Path(env["HOME"]),
            logdir=Path(env.get("REMOTE_CONTROL_LOGDIR", LOGDIR)),
            api_base="https://api.anthropic.com/v1/code",
            keychain_service=env.get("REMOTE_CONTROL_KEYCHAIN_SERVICE",
                                     "Claude Code-credentials"),
            detect_interval=int(env.get("USAGE_LIMIT_DETECT_SECS", "60")),
            resume_interval=int(env.get("USAGE_LIMIT_RESUME_SECS", "300")),
            http_timeout=int(env.get("USAGE_LIMIT_HTTP_TIMEOUT_SECS", "30")),
            gc_age_secs=7 * 24 * 3600,
            backoffs_secs=(5 * 60, 15 * 60, 30 * 60),
            max_attempts=int(env.get("USAGE_LIMIT_MAX_ATTEMPTS", "0")),
            resume_message=env.get("USAGE_LIMIT_RESUME_MESSAGE", "continue"),
            resume_verify_secs=int(env.get("USAGE_LIMIT_VERIFY_SECS", "8")),
            # Default ON: detect + log only, no POST. Set 0/false to actually resume.
            dry_run=_truthy(env.get("USAGE_LIMIT_DRY_RUN", "1")),
            skip_session_ids=skip,
        )


@dataclass(frozen=True)
class ManagerConfig:
    """The session-manager loop (``python3 -m remote_control manager``): the
    autonomous shepherd that reads every session's state and acts on it --
    answer a waiting question, review an idle-looking-done session, or rescue a
    broken one (fork -> resume -> archive).

    Scoped to repos in the allowlist (shares the supervisor's ``active-dirs.txt``
    by default, host-aware) so its blast radius is the repos we already run. The
    grace/cooldown windows keep it from jumping ahead of a human on a fresh
    question or thrashing the same session each tick. DRY_RUN (default ON) plans
    and logs would-be actions without spawning an investigator or calling the API.
    """
    dev: Path
    logdir: Path
    claude_bin: Path
    allowlist_file: Path
    host: str
    interval_secs: int
    # Don't act on a question / idle / disconnect until it has persisted this
    # long -- gives a human first crack and lets a transient blip settle.
    answer_grace_secs: int
    done_grace_secs: int
    rescue_grace_secs: int
    # A "running" worker that has emitted NO event for this long is likely
    # hung/stale (genuinely-progressing sessions fire events) -> flag it actionable
    # instead of trusting the "running" status. Generous, since a single long tool
    # call (build/test) emits nothing while it runs.
    stuck_running_secs: int
    # Min seconds between two manager actions on the *same* session (anti-thrash).
    cooldown_secs: int
    # Cap actions per tick so a misfire can't fan out across every session.
    max_actions_per_tick: int
    # Recency window for the active/idle split (mirrors inventory's TTL).
    ttl_secs: int
    model: Optional[str]
    resume_message: str
    # The investigator (a headless `claude -p`) runs IN the paused session's own
    # bridge worktree, so it runs read-only: ``plan`` lets it explore + answer
    # without editing files the paused agent shares. Bound its runtime so a stuck
    # investigation can't wedge the loop.
    investigator_permission_mode: str
    investigator_timeout_secs: int
    dry_run: bool
    # Separate, off-by-default gate for actually POSTing a structured tool_result
    # answer: the submission shape is still being confirmed against the live API,
    # so even a live (--go) run only investigates + picks until this is enabled.
    submit_enabled: bool
    # A recommendation has two halves: a SESSION half (delivered into the live
    # session -- human-authorized in the ui) and a MANAGER half, which the manager
    # carries out itself via a headless `claude -p` (close-work, fork/resume,
    # archive, ...). The auto-loop runs the MANAGER half. Off by default (shadow:
    # log the would-be command, never spawn) until trusted, mirroring
    # submit_enabled; live auto-execution also needs dry_run off.
    execute_enabled: bool
    # The executor must WRITE (unlike the read-only `plan` investigator), so it
    # runs with this permission mode. Default `acceptEdits`; bump to
    # `bypassPermissions` for fully-unattended git/skill ops -- the global perm-gate
    # hook still vets each tool call either way.
    executor_permission_mode: str
    executor_timeout_secs: int
    skip_session_ids: FrozenSet[str]
    # The guideline doc the investigator is given as policy on every analysis run,
    # and that the manager-ui's "Edit guidelines" button reads/writes.
    guidelines_file: Path

    @property
    def state_file(self) -> Path:
        # NB: deliberately NOT manager.log/-state -- the supervisor owns
        # manager.log (see SupervisorConfig.manager_log).
        return self.logdir / "agent-manager-state.json"

    @property
    def log_file(self) -> Path:
        return self.logdir / "agent-manager.log"

    @property
    def decisions_file(self) -> Path:
        # Append-only JSONL of what the monitor decided each evaluation (for review).
        return self.logdir / "agent-manager-decisions.jsonl"

    @property
    def feedback_file(self) -> Path:
        # Human feedback on the investigator's analyses, keyed by the action
        # signature (``case:session:qhash``) so it sticks to a situation across
        # re-detections. The signal for improving the guidelines. JSON map (upsert).
        return self.logdir / "agent-manager-feedback.json"

    @property
    def scenarios_dir(self) -> Path:
        # Corpus of real ``requires_action_details`` the monitor has seen, for
        # offline ``--replay`` testing. Under the repo (logdir's parent), not logs.
        return self.logdir.parent / "scenarios"

    @property
    def test_cases_dir(self) -> Path:
        # Committed corpus of (input, expected) snapshots for the guideline
        # eval harness (ai-harness#37): written by manager-ui's "Save as test
        # case" button, replayed by ``python -m remote_control eval ...``.
        # Lives under tests/ (in the repo) so cases are reviewable in PRs.
        return self.logdir.parent / "tests" / "manager_cases"

    @classmethod
    def from_env(cls, env: Optional[Mapping[str, str]] = None) -> "ManagerConfig":
        env = os.environ if env is None else env
        host = host_nickname(env)
        skip = frozenset(
            s.strip() for s in env.get("MANAGER_SKIP_SIDS", "").split(",") if s.strip()
        )
        return cls(
            dev=Path(env.get("REMOTE_CONTROL_DEV", DEV)),
            logdir=Path(env.get("REMOTE_CONTROL_LOGDIR", LOGDIR)),
            claude_bin=Path(env.get("REMOTE_CONTROL_CLAUDE_BIN", CLAUDE_BIN)),
            allowlist_file=Path(env.get(
                "MANAGER_ALLOWLIST_FILE",
                env.get("REMOTE_CONTROL_ACTIVE_FILE", ACTIVE_FILE))),
            host=host,
            interval_secs=int(env.get("MANAGER_INTERVAL_SECS", "120")),
            answer_grace_secs=int(env.get("MANAGER_ANSWER_GRACE_SECS", "600")),   # 10m
            done_grace_secs=int(env.get("MANAGER_DONE_GRACE_SECS", "1800")),      # 30m
            rescue_grace_secs=int(env.get("MANAGER_RESCUE_GRACE_SECS", "3600")),  # 1h
            stuck_running_secs=int(env.get("MANAGER_STUCK_RUNNING_SECS", "1800")),  # 30m

            cooldown_secs=int(env.get("MANAGER_COOLDOWN_SECS", "1800")),          # 30m
            max_actions_per_tick=int(env.get("MANAGER_MAX_ACTIONS_PER_TICK", "1")),
            ttl_secs=int(env.get("MANAGER_TTL_SECS", "3600")),
            model=(env.get("MANAGER_MODEL", "").strip() or None),
            resume_message=env.get("MANAGER_RESUME_MESSAGE", "continue"),
            investigator_permission_mode=env.get(
                "MANAGER_INVESTIGATOR_PERMISSION_MODE", "plan"),
            investigator_timeout_secs=int(
                env.get("MANAGER_INVESTIGATOR_TIMEOUT_SECS", "600")),  # 10m
            # Default ON: plan + log only, no investigator spawn / API write.
            dry_run=_truthy(env.get("MANAGER_DRY_RUN", "1")),
            # Default OFF: even live runs only investigate + pick; flip on once the
            # tool_result submission shape is confirmed.
            submit_enabled=_truthy(env.get("MANAGER_SUBMIT_ENABLED", "0")),
            # Default OFF: the MANAGER half of a rec is shadow-logged, not run;
            # set 1 (and dry_run off) to let the loop carry it out via `claude -p`.
            execute_enabled=_truthy(env.get("MANAGER_EXECUTE_ENABLED", "0")),
            executor_permission_mode=env.get(
                "MANAGER_EXECUTOR_PERMISSION_MODE", "acceptEdits"),
            executor_timeout_secs=int(
                env.get("MANAGER_EXECUTOR_TIMEOUT_SECS", "1200")),   # 20m
            skip_session_ids=skip,
            guidelines_file=Path(env.get("MANAGER_GUIDELINES_FILE",
                                         f"{REPO}/docs/session-manager-cases.md")),
        )


@dataclass(frozen=True)
class PermGateConfig:
    """The AI permission gate -- a ``PreToolUse`` hook
    (``python3 -m remote_control perm-gate``) that decides, for each
    tool-permission request, whether to ``allow`` / ``deny`` / ``ask``.

    Two tiers: deterministic **static rules** first (the bulk of calls), then --
    only for the ambiguous middle -- a one-shot **model call** given the stakes
    policy in ``guidelines_file`` (the SAME doc the session-manager evaluates
    against; see ManagerConfig.guidelines_file). The model call goes over the
    Messages API via urllib (no SDK dep, no ``claude -p`` agent spawn) so it can't
    recurse back through this hook.

    Defaults to **shadow mode** (``enforce=False``): it logs the verdict it
    *would* return to ``decisions_file`` but emits no decision, leaving the normal
    permission flow unchanged until the verdicts earn trust -- mirroring how the
    manager shipped (shadow -> review -> flip). Fail-safe throughout: any error
    yields ``ask`` (the human prompt), never a silent ``allow``.
    """
    logdir: Path
    guidelines_file: Path
    keychain_service: str
    api_base: str
    model: str
    max_tokens: int
    http_timeout: int
    # Master switch. Off -> the gate is a no-op (emits nothing; normal flow).
    enabled: bool
    # Off (default) -> SHADOW: decide + log, but emit no decision (no behavior
    # change). On -> the verdict is returned to Claude Code and honored.
    enforce: bool
    # Scopes what ``enforce`` actually binds. Off (default) -> enforce ONLY the
    # deterministic static rules (instant, no model call); the ambiguous middle
    # falls through to the normal human prompt. On -> also bind the AI tier's
    # verdict (a synchronous model call per ambiguous tool call). Lets the safe,
    # zero-latency static enforce ship first, with the AI tier as an opt-in.
    enforce_ai: bool
    # Off -> the ambiguous middle resolves to ``ask`` without any model call
    # (set this to make shadow mode zero-latency/zero-cost: static logging only).
    consult_ai: bool
    # The AI tier classifies each call into a risk tier (green<yellow<orange<red);
    # these two thresholds map the tier to a decision: at/above ``risk_deny_at`` ->
    # deny, at/above ``risk_ask_at`` -> ask, else allow. Defaults: ask at orange,
    # deny at red (so green/yellow auto-allow, orange escalates, red blocks).
    risk_ask_at: str
    risk_deny_at: str

    @property
    def decisions_file(self) -> Path:
        # Append-only JSONL: one record per evaluated tool call (for review).
        return self.logdir / "perm-gate-decisions.jsonl"

    @property
    def log_file(self) -> Path:
        return self.logdir / "perm-gate.log"

    @classmethod
    def from_env(cls, env: Optional[Mapping[str, str]] = None) -> "PermGateConfig":
        env = os.environ if env is None else env
        return cls(
            logdir=Path(env.get("REMOTE_CONTROL_LOGDIR", LOGDIR)),
            guidelines_file=Path(env.get(
                "PERM_GATE_GUIDELINES_FILE",
                env.get("MANAGER_GUIDELINES_FILE",
                        f"{REPO}/docs/session-manager-cases.md"))),
            keychain_service=env.get("REMOTE_CONTROL_KEYCHAIN_SERVICE",
                                     "Claude Code-credentials"),
            api_base=env.get("PERM_GATE_API_BASE", "https://api.anthropic.com/v1"),
            model=env.get("PERM_GATE_MODEL", "claude-haiku-4-5-20251001"),
            max_tokens=int(env.get("PERM_GATE_MAX_TOKENS", "400")),
            http_timeout=int(env.get("PERM_GATE_HTTP_TIMEOUT_SECS", "20")),
            enabled=_truthy(env.get("PERM_GATE_ENABLED", "1")),
            # Default OFF: shadow mode (log only) until the verdicts are trusted.
            enforce=_truthy(env.get("PERM_GATE_ENFORCE", "0")),
            # Default OFF: enforce only the static rules; AI tier stays advisory.
            enforce_ai=_truthy(env.get("PERM_GATE_ENFORCE_AI", "0")),
            consult_ai=_truthy(env.get("PERM_GATE_CONSULT_AI", "1")),
            risk_ask_at=env.get("PERM_GATE_RISK_ASK_AT", "orange").strip().lower(),
            risk_deny_at=env.get("PERM_GATE_RISK_DENY_AT", "red").strip().lower(),
        )


@dataclass(frozen=True)
class CodexConfig:
    """Codex (local CLI) usage-limit monitoring -- the local-process sibling of
    UsageLimitConfig.

    Codex sessions are NOT cloud-hosted: there is no "events" endpoint to POST a
    resume into. Detection reads ``~/.codex/sessions/**/rollout-*.jsonl`` (each
    ``token_count`` event carries a ``rate_limits`` block), and a resume spawns a
    local ``codex exec resume <uuid> "<msg>"`` in the session's recorded cwd.
    """
    codex_home: Path
    codex_bin: Path
    logdir: Path
    enabled: bool
    dry_run: bool
    # used_percent >= this on any window counts as blocked (rate_limit_reached_type
    # being set always counts). 100 = only act on a truly-exhausted window.
    block_threshold: float
    # Ignore rollouts older than this -- a session whose log went quiet long ago
    # is abandoned, not waiting-to-resume. Generous enough to cover the 5h window.
    recent_secs: int
    detect_interval: int
    resume_interval: int
    backoffs_secs: Tuple[int, ...]
    max_attempts: int
    # A spawned `codex exec resume` runs the whole turn; treat its pid as alive
    # (don't re-fire) until it exits or this long elapses, whichever is first.
    max_runtime: int
    gc_age_secs: int
    resume_message: str
    skip_session_ids: FrozenSet[str]

    @property
    def sessions_dir(self) -> Path:
        return self.codex_home / "sessions"

    @property
    def index_file(self) -> Path:
        return self.codex_home / "session_index.jsonl"

    @property
    def state_file(self) -> Path:
        return self.logdir / "paused-codex-sessions.json"

    @property
    def resume_logdir(self) -> Path:
        return self.logdir / "codex-resume"

    @classmethod
    def from_env(cls, env: Optional[Mapping[str, str]] = None) -> "CodexConfig":
        env = os.environ if env is None else env
        skip = frozenset(
            s.strip() for s in env.get("USAGE_LIMIT_CODEX_SKIP_SIDS", "").split(",")
            if s.strip()
        )
        home = Path(env["HOME"])
        return cls(
            codex_home=Path(env.get("CODEX_HOME", str(home / ".codex"))),
            codex_bin=Path(env.get(
                "REMOTE_CONTROL_CODEX_BIN",
                "/Applications/Codex.app/Contents/Resources/codex")),
            logdir=Path(env.get("REMOTE_CONTROL_LOGDIR", LOGDIR)),
            enabled=_truthy(env.get("USAGE_LIMIT_CODEX_ENABLED", "1")),
            # Shares the global dry-run switch with the Claude monitor.
            dry_run=_truthy(env.get("USAGE_LIMIT_DRY_RUN", "1")),
            block_threshold=float(env.get("USAGE_LIMIT_CODEX_THRESHOLD", "100")),
            recent_secs=int(env.get("USAGE_LIMIT_CODEX_RECENT_SECS", str(6 * 3600))),
            detect_interval=int(env.get("USAGE_LIMIT_DETECT_SECS", "60")),
            resume_interval=int(env.get("USAGE_LIMIT_RESUME_SECS", "300")),
            backoffs_secs=(5 * 60, 15 * 60, 30 * 60),
            max_attempts=int(env.get("USAGE_LIMIT_MAX_ATTEMPTS", "0")),
            max_runtime=int(env.get("USAGE_LIMIT_CODEX_MAX_RUNTIME", "1800")),
            gc_age_secs=7 * 24 * 3600,
            resume_message=env.get("USAGE_LIMIT_RESUME_MESSAGE", "continue"),
            skip_session_ids=skip,
        )
