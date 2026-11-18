"""
Empirical adversarial tests for Milestone 6: Deployment Infrastructure and Healthcheck.
Authored by Challenger 2 (challenger_m6_2).
"""

import os
import re
import stat
import subprocess
import sys
import tempfile
import time
import unittest
import yaml


class TestHealthcheckCLI(unittest.TestCase):
    """Empirical tests for bot/healthcheck.py covering all states and edge cases."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.temp_dir.name, "test.sqlite3")
        self.hb_path = os.path.join(self.temp_dir.name, "heartbeat")

    def tearDown(self):
        self.temp_dir.cleanup()

    def _create_valid_db(self, path: str):
        import sqlite3
        conn = sqlite3.connect(path)
        conn.execute("CREATE TABLE IF NOT EXISTS probe (id INTEGER PRIMARY KEY);")
        conn.commit()
        conn.close()

    def _create_heartbeat(self, path: str, offset_seconds: float = 0.0):
        with open(path, "w") as f:
            f.write("heartbeat\n")
        mtime = time.time() - offset_seconds
        os.utime(path, (mtime, mtime))

    def _run_healthcheck(self, db_path=None, hb_path=None, extra_args=None, env=None):
        cmd = [sys.executable, "-m", "bot.healthcheck"]
        if db_path is not None:
            cmd.extend(["--db", db_path])
        if hb_path is not None:
            cmd.extend(["--heartbeat", hb_path])
        if extra_args:
            cmd.extend(extra_args)

        run_env = os.environ.copy()
        if env:
            run_env.update(env)

        return subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=run_env,
        )

    def test_state_a_valid_db_fresh_heartbeat(self):
        """State A: Valid SQLite database + Fresh heartbeat (mtime = current time) -> Must exit 0."""
        self._create_valid_db(self.db_path)
        self._create_heartbeat(self.hb_path, offset_seconds=0.0)

        res = self._run_healthcheck(self.db_path, self.hb_path)
        self.assertEqual(res.returncode, 0, f"Expected exit 0, got {res.returncode}. Stderr: {res.stderr}")
        self.assertIn("[HEALTHCHECK OK]", res.stdout)

    def test_state_b_valid_db_boundary_heartbeat(self):
        """State B: Valid SQLite database + Boundary heartbeat (mtime = time.time() - 179.0s) -> Must exit 0."""
        self._create_valid_db(self.db_path)
        self._create_heartbeat(self.hb_path, offset_seconds=179.0)

        res = self._run_healthcheck(self.db_path, self.hb_path)
        self.assertEqual(res.returncode, 0, f"Expected exit 0 for 179s age, got {res.returncode}. Stderr: {res.stderr}")
        self.assertIn("[HEALTHCHECK OK]", res.stdout)

    def test_state_c_valid_db_stale_heartbeat(self):
        """State C: Valid SQLite database + Stale heartbeat (mtime = time.time() - 181.0s) -> Must exit 1."""
        self._create_valid_db(self.db_path)
        self._create_heartbeat(self.hb_path, offset_seconds=181.0)

        res = self._run_healthcheck(self.db_path, self.hb_path)
        self.assertEqual(res.returncode, 1, f"Expected exit 1 for 181s age, got {res.returncode}")
        self.assertIn("[HEALTHCHECK FAIL]", res.stderr)
        self.assertIn("stale", res.stderr.lower())

    def test_state_d_missing_database_file(self):
        """State D: Missing database file -> Must exit 1."""
        missing_db = os.path.join(self.temp_dir.name, "nonexistent.sqlite3")
        self._create_heartbeat(self.hb_path, offset_seconds=0.0)

        res = self._run_healthcheck(missing_db, self.hb_path)
        self.assertEqual(res.returncode, 1, f"Expected exit 1 for missing DB, got {res.returncode}")
        self.assertIn("[HEALTHCHECK FAIL]", res.stderr)
        self.assertIn("not found", res.stderr.lower())

    def test_state_e_corrupt_non_sqlite_database_file(self):
        """State E: Corrupt non-SQLite database file -> Must exit 1."""
        with open(self.db_path, "wb") as f:
            f.write(b"NOT_A_SQLITE_DATABASE_CORRUPT_BYTES_XYZ_1234567890")
        self._create_heartbeat(self.hb_path, offset_seconds=0.0)

        res = self._run_healthcheck(self.db_path, self.hb_path)
        self.assertEqual(res.returncode, 1, f"Expected exit 1 for corrupt DB, got {res.returncode}")
        self.assertIn("[HEALTHCHECK FAIL]", res.stderr)

    def test_state_f_missing_heartbeat_file(self):
        """State F: Missing heartbeat file -> Must exit 1."""
        self._create_valid_db(self.db_path)
        missing_hb = os.path.join(self.temp_dir.name, "nonexistent_heartbeat")

        res = self._run_healthcheck(self.db_path, missing_hb)
        self.assertEqual(res.returncode, 1, f"Expected exit 1 for missing heartbeat, got {res.returncode}")
        self.assertIn("[HEALTHCHECK FAIL]", res.stderr)
        self.assertIn("not found", res.stderr.lower())

    def test_state_g_unreadable_heartbeat_permissions_error(self):
        """State G: Unreadable heartbeat file / permissions error -> Must exit 1."""
        self._create_valid_db(self.db_path)
        unreadable_dir = os.path.join(self.temp_dir.name, "no_access_dir")
        os.makedirs(unreadable_dir, exist_ok=True)
        unreadable_hb = os.path.join(unreadable_dir, "heartbeat")
        with open(unreadable_hb, "w") as f:
            f.write("test")

        # Strip read/execute permissions from directory so stat on file fails
        try:
            os.chmod(unreadable_dir, 0o000)
            res = self._run_healthcheck(self.db_path, unreadable_hb)
            self.assertEqual(res.returncode, 1, f"Expected exit 1 on unreadable path, got {res.returncode}")
            self.assertIn("[HEALTHCHECK FAIL]", res.stderr)
        finally:
            os.chmod(unreadable_dir, 0o755)

    def test_adversarial_db_locked_timeout(self):
        """Adversarial: Database is locked by an exclusive transaction; check times out -> Must exit 1."""
        import sqlite3
        self._create_valid_db(self.db_path)
        self._create_heartbeat(self.hb_path, offset_seconds=0.0)

        # Acquire exclusive lock
        lock_conn = sqlite3.connect(self.db_path, timeout=0.1)
        lock_conn.execute("BEGIN EXCLUSIVE TRANSACTION;")
        try:
            res = self._run_healthcheck(self.db_path, self.hb_path, extra_args=["--timeout", "0.2"])
            self.assertEqual(res.returncode, 1, f"Expected exit 1 on locked DB, got {res.returncode}")
            self.assertIn("[HEALTHCHECK FAIL]", res.stderr)
        finally:
            lock_conn.rollback()
            lock_conn.close()

    def test_adversarial_db_is_directory(self):
        """Adversarial: Database path points to a directory -> Must exit 1."""
        dir_db = os.path.join(self.temp_dir.name, "dir_as_db.sqlite3")
        os.makedirs(dir_db, exist_ok=True)
        self._create_heartbeat(self.hb_path, offset_seconds=0.0)

        res = self._run_healthcheck(dir_db, self.hb_path)
        self.assertEqual(res.returncode, 1, f"Expected exit 1 when DB is directory, got {res.returncode}")
        self.assertIn("[HEALTHCHECK FAIL]", res.stderr)

    def test_adversarial_heartbeat_future_mtime(self):
        """Adversarial: Heartbeat with future mtime (e.g. clock drift) -> Must exit 0 (age is <= 180)."""
        self._create_valid_db(self.db_path)
        # mtime 60 seconds into the future
        self._create_heartbeat(self.hb_path, offset_seconds=-60.0)

        res = self._run_healthcheck(self.db_path, self.hb_path)
        self.assertEqual(res.returncode, 0, f"Expected exit 0 for future mtime, got {res.returncode}")
        self.assertIn("[HEALTHCHECK OK]", res.stdout)

    def test_adversarial_env_var_resolution(self):
        """Adversarial: Check path resolution via DATABASE_PATH and HEARTBEAT_PATH environment variables."""
        self._create_valid_db(self.db_path)
        self._create_heartbeat(self.hb_path, offset_seconds=0.0)

        env = {
            "DATABASE_PATH": self.db_path,
            "HEARTBEAT_PATH": self.hb_path,
        }
        # Run without --db or --heartbeat CLI flags
        res = self._run_healthcheck(db_path=None, hb_path=None, env=env)
        self.assertEqual(res.returncode, 0, f"Expected exit 0 with env vars, got {res.returncode}. Stderr: {res.stderr}")
        self.assertIn("[HEALTHCHECK OK]", res.stdout)

    def test_adversarial_custom_max_age_flag(self):
        """Adversarial: Test custom --max-age CLI argument."""
        self._create_valid_db(self.db_path)
        self._create_heartbeat(self.hb_path, offset_seconds=70.0)

        # With default 180s, 70s passes
        res_default = self._run_healthcheck(self.db_path, self.hb_path)
        self.assertEqual(res_default.returncode, 0)

        # With --max-age 60, 70s must fail
        res_custom = self._run_healthcheck(self.db_path, self.hb_path, extra_args=["--max-age", "60.0"])
        self.assertEqual(res_custom.returncode, 1)
        self.assertIn("[HEALTHCHECK FAIL]", res_custom.stderr)


class TestComposeHardening(unittest.TestCase):
    """Empirical tests for compose.yaml security constraints and resource limits."""

    @classmethod
    def setUpClass(cls):
        compose_path = os.path.join(os.getcwd(), "compose.yaml")
        with open(compose_path, "r", encoding="utf-8") as f:
            cls.raw_content = f.read()
            cls.compose_data = yaml.safe_load(cls.raw_content)

    def test_compose_parses_cleanly(self):
        """Assert compose.yaml parses into a valid dict structure."""
        self.assertIsInstance(self.compose_data, dict)
        self.assertIn("services", self.compose_data)
        self.assertIn("dtek-bot", self.compose_data["services"])

    def test_no_ports_or_expose_in_service(self):
        """Assert ports and expose are NOT in the service definition."""
        service = self.compose_data["services"]["dtek-bot"]
        self.assertNotIn("ports", service, "compose.yaml service MUST NOT define 'ports'")
        self.assertNotIn("expose", service, "compose.yaml service MUST NOT define 'expose'")

    def test_no_ports_or_expose_in_entire_file(self):
        """Assert ports and expose directives do not appear anywhere in compose.yaml."""
        lines = self.raw_content.splitlines()
        for idx, line in enumerate(lines, start=1):
            stripped = line.strip()
            # Ignore comments
            if stripped.startswith("#"):
                continue
            self.assertFalse(
                re.match(r"^ports\s*:", stripped, re.IGNORECASE),
                f"Forbidden 'ports:' directive found at line {idx}: {line}",
            )
            self.assertFalse(
                re.match(r"^expose\s*:", stripped, re.IGNORECASE),
                f"Forbidden 'expose:' directive found at line {idx}: {line}",
            )

    def test_read_only_rootfs_is_present(self):
        """Assert read_only: true is present in service definition."""
        service = self.compose_data["services"]["dtek-bot"]
        self.assertIn("read_only", service)
        self.assertTrue(service["read_only"])

    def test_tmpfs_configured_with_tmp(self):
        """Assert tmpfs is configured with /tmp."""
        service = self.compose_data["services"]["dtek-bot"]
        self.assertIn("tmpfs", service)
        tmpfs_entries = service["tmpfs"]
        if isinstance(tmpfs_entries, list):
            found_tmp = any("/tmp" in str(entry) for entry in tmpfs_entries)
        elif isinstance(tmpfs_entries, dict):
            found_tmp = "/tmp" in tmpfs_entries
        else:
            found_tmp = "/tmp" in str(tmpfs_entries)
        self.assertTrue(found_tmp, f"tmpfs /tmp not found in tmpfs config: {tmpfs_entries}")

    def test_resource_limits_mem_and_cpus(self):
        """Assert mem_limit: 256m and cpus: 0.5 (or '0.5') are present."""
        service = self.compose_data["services"]["dtek-bot"]
        self.assertIn("mem_limit", service)
        self.assertEqual(str(service["mem_limit"]).lower(), "256m")

        self.assertIn("cpus", service)
        self.assertIn(str(service["cpus"]), ["0.5", 0.5])

    def test_restart_policy_and_volume(self):
        """Assert restart policy and persistent volume mount."""
        service = self.compose_data["services"]["dtek-bot"]
        self.assertEqual(service.get("restart"), "unless-stopped")
        volumes = service.get("volumes", [])
        self.assertTrue(any("/app/data" in str(v) for v in volumes))


class TestContainerfile(unittest.TestCase):
    """Empirical tests for Containerfile instructions and security bounds."""

    @classmethod
    def setUpClass(cls):
        containerfile_path = os.path.join(os.getcwd(), "Containerfile")
        with open(containerfile_path, "r", encoding="utf-8") as f:
            cls.raw_content = f.read()
            cls.lines = cls.raw_content.splitlines()

    def test_zero_expose_instructions(self):
        """Assert NO instruction starts with EXPOSE (case-insensitive)."""
        for idx, line in enumerate(self.lines, start=1):
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            self.assertFalse(
                re.match(r"^EXPOSE\b", stripped, re.IGNORECASE),
                f"Forbidden EXPOSE instruction found at line {idx}: {line}",
            )

    def test_user_directive_sets_botuser_1000(self):
        """Assert USER directive sets UID 1000 (botuser)."""
        user_instructions = [
            line.strip() for line in self.lines if re.match(r"^USER\b", line.strip(), re.IGNORECASE)
        ]
        self.assertTrue(len(user_instructions) > 0, "No USER directive found in Containerfile")
        last_user = user_instructions[-1]
        self.assertIn("botuser", last_user)

        # Also assert UID 1000 is defined in useradd
        self.assertTrue(
            any("1000" in line and "botuser" in line for line in self.lines),
            "Expected UID 1000 associated with botuser in Containerfile",
        )

    def test_healthcheck_calls_bot_healthcheck(self):
        """Assert HEALTHCHECK instruction calls python -m bot.healthcheck."""
        healthcheck_lines = [
            line.strip() for line in self.lines if "HEALTHCHECK" in line.upper()
        ]
        self.assertTrue(len(healthcheck_lines) > 0, "No HEALTHCHECK instruction found")
        # In Containerfile, CMD ["python", "-m", "bot.healthcheck"] follows HEALTHCHECK
        full_text = " ".join(self.lines)
        self.assertIn("python", full_text)
        self.assertIn("bot.healthcheck", full_text)
        self.assertRegex(full_text, r'["\']?python["\']?,\s*["\']?-m["\']?,\s*["\']?bot\.healthcheck["\']?')

    def test_multistage_build_and_python_311(self):
        """Assert multi-stage build using python:3.11-slim."""
        from_lines = [line.strip() for line in self.lines if line.strip().upper().startswith("FROM")]
        self.assertGreaterEqual(len(from_lines), 2, "Expected at least 2 stages in Containerfile")
        for f in from_lines:
            self.assertIn("python:3.11-slim", f)


class TestGitHubPagesWorkflow(unittest.TestCase):
    """Empirical tests for .github/workflows/pages.yml."""

    @classmethod
    def setUpClass(cls):
        workflow_path = os.path.join(os.getcwd(), ".github", "workflows", "pages.yml")
        with open(workflow_path, "r", encoding="utf-8") as f:
            cls.raw_content = f.read()
            cls.data = yaml.safe_load(cls.raw_content)

    def test_workflow_yaml_validity(self):
        """Assert workflow parses into a dictionary with required top-level keys."""
        self.assertIsInstance(self.data, dict)
        self.assertIn("name", self.data)
        # Note: in YAML 1.1 (PyYAML), unquoted `on:` parses as boolean True
        self.assertTrue("on" in self.data or True in self.data)
        self.assertIn("permissions", self.data)
        self.assertIn("jobs", self.data)

    def test_workflow_triggers(self):
        """Assert triggers include push to main on docs/** and workflow_dispatch."""
        triggers = self.data.get("on") if "on" in self.data else self.data.get(True)
        self.assertIsNotNone(triggers, "Workflow triggers ('on' / True) must be present")
        self.assertIn("push", triggers)
        push_trigger = triggers["push"]
        self.assertIn("branches", push_trigger)
        self.assertIn("main", push_trigger["branches"])
        self.assertIn("paths", push_trigger)
        self.assertTrue(any("docs" in str(p) for p in push_trigger["paths"]))
        self.assertIn("workflow_dispatch", triggers)

    def test_workflow_permissions(self):
        """Assert permissions include contents: read, pages: write, id-token: write."""
        perms = self.data["permissions"]
        self.assertEqual(perms.get("contents"), "read")
        self.assertEqual(perms.get("pages"), "write")
        self.assertEqual(perms.get("id-token"), "write")

    def test_workflow_deployment_steps(self):
        """Assert deployment steps use standard actions and upload docs directory."""
        jobs = self.data["jobs"]
        self.assertIn("deploy", jobs)
        deploy_job = jobs["deploy"]
        steps = deploy_job.get("steps", [])

        actions_used = [s.get("uses", "") for s in steps]
        self.assertTrue(any("actions/checkout" in a for a in actions_used))
        self.assertTrue(any("actions/configure-pages" in a for a in actions_used))
        self.assertTrue(any("actions/upload-pages-artifact" in a for a in actions_used))
        self.assertTrue(any("actions/deploy-pages" in a for a in actions_used))

        # Check path for upload-pages-artifact is docs
        upload_step = next(s for s in steps if "actions/upload-pages-artifact" in s.get("uses", ""))
        self.assertEqual(upload_step.get("with", {}).get("path"), "docs")


class TestStaticSecurityAudit(unittest.TestCase):
    """Static security and placeholder check across all Milestone 6 targets."""

    TARGET_FILES = [
        "bot/healthcheck.py",
        "Containerfile",
        "compose.yaml",
        ".github/workflows/pages.yml",
    ]

    def test_zero_placeholders(self):
        """Assert 0 occurrences of 'TODO' or 'implement here' across target files."""
        for rel_path in self.TARGET_FILES:
            full_path = os.path.join(os.getcwd(), rel_path)
            self.assertTrue(os.path.isfile(full_path), f"File {rel_path} does not exist")
            with open(full_path, "r", encoding="utf-8") as f:
                content = f.read()

            self.assertNotIn("TODO", content.upper(), f"Found 'TODO' in {rel_path}")
            self.assertNotIn("IMPLEMENT HERE", content.upper(), f"Found 'implement here' in {rel_path}")


if __name__ == "__main__":
    unittest.main()
