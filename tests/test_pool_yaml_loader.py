"""Unit tests for scripts/pool/_yaml_to_env.py — the YAML → shell-export helper.

Each case writes a tiny YAML to a tmpfile, invokes the helper as a subprocess,
and checks the printed lines. Then it eval's them in bash and asserts the
resulting variables (so we cover the round-trip the adapters actually rely on).
"""
import os
import shutil
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_HELPER = _ROOT / "scripts" / "pool" / "_yaml_to_env.py"


def _emit(yaml_text: str) -> list[str]:
    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as f:
        f.write(yaml_text)
        path = f.name
    try:
        out = subprocess.check_output(["python3", str(_HELPER), path], text=True)
    finally:
        os.unlink(path)
    return [ln for ln in out.splitlines() if ln]


def _eval_in_bash(yaml_text: str, probe: str) -> str:
    """Pipe the helper through `bash -c` and run a probe command after eval."""
    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as f:
        f.write(yaml_text)
        path = f.name
    try:
        cmd = f'eval "$(python3 {_HELPER} {path})"; {probe}'
        out = subprocess.check_output(["bash", "-c", cmd], text=True)
    finally:
        os.unlink(path)
    return out.strip()


class EmitTest(unittest.TestCase):
    def test_scalars_at_root(self):
        lines = _emit("name: app-one\nadapter: laravel-docker\n")
        self.assertIn("NAME=app-one", lines)
        self.assertIn("ADAPTER=laravel-docker", lines)

    def test_nested_map_flattens_with_underscore(self):
        lines = _emit(textwrap.dedent("""\
            db:
              prefix: app_one
              test_host_port: 3315
        """))
        self.assertIn("DB_PREFIX=app_one", lines)
        self.assertIn("DB_TEST_HOST_PORT=3315", lines)

    def test_list_becomes_bash_array(self):
        lines = _emit("slots: [pool0, pool1, pool2]\n")
        self.assertIn("SLOTS=(pool0 pool1 pool2)", lines)

    def test_tilde_expands_to_home(self):
        lines = _emit("state_dir: ~/.example-pool\n")
        expanded = os.path.expanduser("~/.example-pool")
        self.assertIn(f"STATE_DIR={expanded}", lines)

    def test_values_with_spaces_are_quoted(self):
        # shlex.quote wraps with single quotes when needed.
        lines = _emit("label: with spaces\n")
        self.assertIn("LABEL='with spaces'", lines)

    def test_backslash_in_value_round_trips(self):
        # Laravel seeder class name pattern: single backslashes in YAML
        # (single-quoted YAML treats `\` literally) must survive the bash
        # eval round-trip without being interpreted as escapes.
        out = _eval_in_bash(
            "seeder: 'Database\\Seeders\\TestBaselineSeeder'\n",
            'printf %s "$SEEDER"',
        )
        self.assertEqual(out, "Database\\Seeders\\TestBaselineSeeder")


class BashRoundTripTest(unittest.TestCase):
    """End-to-end: pipe → eval → read back the resulting bash vars."""

    def test_array_round_trip(self):
        out = _eval_in_bash(
            textwrap.dedent("""\
                pools:
                  dev:
                    slots: [pool0, pool1, pool2, pool3, pool4, pool5]
                    offsets: [20, 21, 22, 23, 24, 25]
            """),
            'printf "%s\\n" "${POOLS_DEV_SLOTS[@]}" "${POOLS_DEV_OFFSETS[@]}"',
        )
        self.assertEqual(
            out.splitlines(),
            ["pool0", "pool1", "pool2", "pool3", "pool4", "pool5",
             "20", "21", "22", "23", "24", "25"],
        )

    def test_scalar_round_trip(self):
        out = _eval_in_bash(
            "app:\n  main_dir: /tmp/app\n  container_prefix: example\n",
            'printf "%s|%s" "$APP_MAIN_DIR" "$APP_CONTAINER_PREFIX"',
        )
        self.assertEqual(out, "/tmp/app|example")


class ErrorTest(unittest.TestCase):
    def test_missing_arg_errors(self):
        rc = subprocess.run(
            ["python3", str(_HELPER)], capture_output=True
        ).returncode
        self.assertEqual(rc, 2)

    def test_non_mapping_root_errors(self):
        with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as f:
            f.write("- just\n- a\n- list\n")
            path = f.name
        try:
            rc = subprocess.run(
                ["python3", str(_HELPER), path], capture_output=True
            ).returncode
            self.assertEqual(rc, 2)
        finally:
            os.unlink(path)


if __name__ == "__main__":
    if shutil.which("bash") is None:  # pragma: no cover
        raise SystemExit("bash required for these tests")
    unittest.main()
