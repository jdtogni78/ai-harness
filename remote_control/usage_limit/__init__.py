"""Auto-resume Claude Code sessions paused by the cloud usage/session limit.

v2 (API-based): talks to the claude.ai code-sessions API rather than scanning
local JSONL transcripts (which never touched the cloud/bridge session the app
shows). ``detect`` holds the pure filter/parse/selection logic; ``monitor``
holds the API client, state/lock, loop, and ``main``.
"""
