"""Tests for the root-level ``perm_gate_lab`` package.

Named ``perm_gate_lab_tests`` rather than ``perm_gate_lab`` on purpose: under
``python3 -m unittest discover -s tests`` (without ``-t .``) the ``tests``
directory itself lands on ``sys.path``, so a subpackage sharing the name of a
root package shadows it and ``from perm_gate_lab.redact import ...`` dies with
a bare ModuleNotFoundError. The distinct name keeps discovery working under
both the canonical runner and the shorthand one.
"""
