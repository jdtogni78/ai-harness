"""Launchd-managed services for the ai-harness host.

A small, stdlib-only package (targets the system /usr/bin/python3) that replaces
the former zsh scripts:

  - ``supervisor``     -- one ``claude remote-control`` server per allowlisted dir
  - ``usage-monitor``  -- auto-resume sessions paused by the cloud usage limit
  - ``install``        -- (re)bootstrap the LaunchAgents in this repo

Run a service with ``python3 -m remote_control <name>``.

Design notes:
  - stdlib only (launchd runs these with a bare environment),
  - pure decision logic split from side effects so it is unit-testable,
  - no module-level work: importing any module is side-effect free.
"""
