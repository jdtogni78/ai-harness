"""status_mcp — cos-console data plane.

Assembles a StatusReport (see ../SCHEMA.md) for a project from real,
read-only signals this host already has: GitHub Projects boards, Maven
test artifacts, deploy logs, narrate-demo/visual-review artifacts, and
decisions mined from git merges + close-work briefs.

Read-only w.r.t. every real system. Never deploys, migrates, or mutates.
"""

__version__ = "1.0.0"
SCHEMA_VERSION = "1.0"
