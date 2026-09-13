"""Tests for context-diet. Uses a temporary fake home; never touches the real ~/."""
import datetime as dt
import importlib.util
import io
import json
import os
import shutil
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
CLI = os.path.join(os.path.dirname(HERE), "context_diet.py")
spec = importlib.util.spec_from_file_location("context_diet", CLI)
cd = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cd)


def _write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)


def _skill_md(name, desc):
    return f"---\nname: {name}\ndescription: {desc}\n---\n\n# {name}\n\nBody text for {name}.\n"


DESC_A = "A" * 400          # 400 chars -> ~100 tokens + name overhead
DESC_B = "B" * 200
DESC_C = "C" * 80
DESC_P1 = "P" * 120
DESC_P2 = "Q" * 40
SECRET_URL = "https://mcp.example.com/sse?secret=deadbeefcafebabe0123456789abcdef&x=1"
CLEAN_URL = "https://mcp.clean.example.org/mcp"


def make_fake_home(root):
    home = os.path.join(root, "home")
    project = os.path.join(root, "project")
    os.makedirs(project)
    # 3 user skills
    _write(os.path.join(home, ".claude", "skills", "alpha", "SKILL.md"), _skill_md("alpha", DESC_A))
    _write(os.path.join(home, ".claude", "skills", "beta", "SKILL.md"), _skill_md("beta", DESC_B))
    _write(os.path.join(home, ".claude", "skills", "gamma", "SKILL.md"), _skill_md("gamma", DESC_C))
    # 1 plugin with 2 skills
    plug_path = os.path.join(home, ".claude", "plugins", "cache", "mkt", "myplug", "1.0.0")
    _write(os.path.join(plug_path, "skills", "p-one", "SKILL.md"), _skill_md("p-one", DESC_P1))
    _write(os.path.join(plug_path, "skills", "p-two", "SKILL.md"), _skill_md("p-two", DESC_P2))
    _write(os.path.join(home, ".claude", "plugins", "installed_plugins.json"), json.dumps({
        "version": 2,
        "plugins": {"myplug@mkt": [{"scope": "user", "installPath": plug_path, "version": "1.0.0"}]},
    }))
    _write(os.path.join(home, ".claude", "settings.json"), json.dumps({
        "model": "opus", "enabledPlugins": {"myplug@mkt": True}, "theme": "dark"
    }, indent=2))
    # 2 MCP servers, one with a secret in the URL
    _write(os.path.join(home, ".claude.json"), json.dumps({
        "mcpServers": {
            "secretsrv": {"type": "http", "url": SECRET_URL},
            "cleansrv": {"type": "http", "url": CLEAN_URL},
        },
        "projects": {project: {"mcpServers": {}}},
    }))
    # history: only "beta" used (recently); cleansrv used; a listing line that
    # mentions every skill must NOT count; an old alpha use outside the window.
    now = dt.datetime.now(tz=dt.timezone.utc)
    recent = now - dt.timedelta(days=2)
    old = now - dt.timedelta(days=60)
    lines = [
        json.dumps({"display": "/beta please", "timestamp": int(recent.timestamp() * 1000), "project": project}),
        json.dumps({"display": "unrelated /Users/x/alpha/file.txt path", "timestamp": int(recent.timestamp() * 1000)}),
    ]
    _write(os.path.join(home, ".claude", "history.jsonl"), "\n".join(lines) + "\n")
    tlines = [
        json.dumps({"type": "assistant", "timestamp": recent.isoformat().replace("+00:00", "Z"),
                    "message": {"content": [{"type": "tool_use", "name": "Skill", "input": {"skill": "beta"}}]}}),
        json.dumps({"type": "assistant", "timestamp": recent.isoformat().replace("+00:00", "Z"),
                    "message": {"content": [{"type": "tool_use", "name": "mcp__cleansrv__do_thing", "input": {}}]}}),
        json.dumps({"type": "attachment", "timestamp": recent.isoformat().replace("+00:00", "Z"),
                    "rendered": [{"content": "<system-reminder>\nThe following skills are available for use with the Skill tool:\n\n- alpha: use /alpha\n- gamma: gamma\n- myplug:p-one: /p-one\n</system-reminder>"}]}),
        json.dumps({"type": "user", "timestamp": old.isoformat().replace("+00:00", "Z"),
                    "message": {"content": "<command-name>/alpha</command-name>"}}),
        json.dumps({"type": "user", "timestamp": recent.isoformat().replace("+00:00", "Z"),
                    "message": {"content": "CONTEXT_DIET_REPORT /gamma /p-two myplug:p-two \"skill\":\"gamma\""}}),
    ]
    _write(os.path.join(home, ".claude", "projects", "-proj", "sess.jsonl"), "\n".join(tlines) + "\n")
    return home, project


class ContextDietTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="context-diet-test-")
        self.home, self.project = make_fake_home(self.tmp)
        self.real_home = os.path.expanduser("~")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _run(self, *extra):
        out = io.StringIO()
        rc = cd.run(["--home", self.home, "--project", self.project, "--days", "14", *extra], out=out)
        self.assertEqual(rc, 0)
        return out.getvalue()

    def _json(self, *extra):
        return json.loads(self._run("--json", *extra))

    def test_discovery_counts(self):
        data = self._json()
        self.assertEqual(data["summary"]["skills_total"], 5)
        sources = sorted(s["source"] for s in data["skills"])
        self.assertEqual(sources, ["plugin:myplug", "plugin:myplug", "user", "user", "user"])
        self.assertEqual(data["summary"]["mcp_servers_total"], 2)
        self.assertEqual(len(data["plugins"]), 1)
        self.assertEqual(data["plugins"][0]["skills_found"], 2)
        invoke = {s["invoke_name"] for s in data["skills"]}
        self.assertIn("myplug:p-one", invoke)
        self.assertIn("alpha", invoke)

    def test_token_estimates(self):
        data = self._json()
        by = {s["name"]: s for s in data["skills"]}
        self.assertEqual(by["alpha"]["description_chars"], 400)
        self.assertEqual(by["alpha"]["tokens_per_turn_est"], cd.est_tokens(400 + len("alpha") + 4))
        self.assertEqual(by["p-one"]["tokens_per_turn_est"], cd.est_tokens(120 + len("myplug:p-one") + 4))
        self.assertGreater(by["alpha"]["skill_md_tokens_est"], by["alpha"]["tokens_per_turn_est"])
        self.assertEqual(data["summary"]["tokens_per_turn_est_total"],
                         sum(s["tokens_per_turn_est"] for s in data["skills"]))
        self.assertEqual(cd.est_tokens(0), 0)
        self.assertEqual(cd.est_tokens(9), 3)

    def test_unused_detection(self):
        data = self._json()
        by = {s["name"]: s for s in data["skills"]}
        self.assertEqual(by["beta"]["uses_in_window"], 2)   # history + Skill tool_use
        self.assertIsNotNone(by["beta"]["last_used"])
        self.assertEqual(by["alpha"]["uses_in_window"], 0)  # only an old use + a path mention
        self.assertEqual(by["alpha"]["uses_total"], 1)      # the old <command-name> hit
        self.assertEqual(by["gamma"]["uses_in_window"], 0)  # only in listing / marker lines
        self.assertEqual(by["gamma"]["uses_total"], 0)
        self.assertEqual(by["p-one"]["uses_total"], 0)
        self.assertEqual(by["p-two"]["uses_total"], 0)
        self.assertEqual(data["summary"]["skills_unused"], 4)
        mcp = {m["name"]: m for m in data["mcp_servers"]}
        self.assertEqual(mcp["cleansrv"]["uses_in_window"], 1)
        self.assertEqual(mcp["secretsrv"]["uses_in_window"], 0)
        self.assertEqual(data["summary"]["mcp_servers_unused"], 1)

    def test_secret_redaction(self):
        text = self._run()
        js = self._run("--json")
        for blob in (text, js):
            self.assertNotIn("secret=", blob)
            self.assertNotIn("deadbeef", blob)
            self.assertIn("mcp.example.com", blob)
        data = json.loads(js)
        mcp = {m["name"]: m for m in data["mcp_servers"]}
        self.assertTrue(mcp["secretsrv"]["url_redacted"])
        self.assertEqual(mcp["secretsrv"]["url_host"], "https://mcp.example.com [redacted]")
        self.assertFalse(mcp["cleansrv"]["url_redacted"])
        self.assertEqual(mcp["cleansrv"]["url_host"], "https://mcp.clean.example.org")
        self.assertTrue(cd.redact_url("https://h.io/x?token=abc")[1])
        self.assertTrue(cd.redact_url("https://h.io/x?api_key=abc")[1])
        self.assertTrue(cd.redact_url("https://h.io/0123456789abcdef0123/sse")[1])

    def test_disable_list_contents(self):
        plan_path = os.path.join(self.tmp, "plan.json")
        text = self._run("--disable-list", plan_path)
        self.assertIn("not applied", text)
        with open(plan_path) as fh:
            plan = json.load(fh)
        self.assertFalse(plan["applied"])
        self.assertEqual([p["key"] for p in plan["plugins_to_disable"]], ["myplug@mkt"])
        moved = sorted(m["name"] for m in plan["skill_dirs_to_move"])
        self.assertEqual(moved, ["alpha", "gamma"])
        for m in plan["skill_dirs_to_move"]:
            self.assertTrue(m["to"].startswith(os.path.join(self.home, ".claude", "skills-disabled")))
            self.assertIn("never delete", m["action"])
        self.assertEqual([m["name"] for m in plan["mcp_servers_unused_informational"]], ["secretsrv"])
        self.assertNotIn("secret=", json.dumps(plan))
        self.assertEqual(plan["estimated_tokens_per_turn_saved"],
                         sum(p["tokens_per_turn_est"] for p in plan["plugins_to_disable"])
                         + sum(m["tokens_per_turn_est"] for m in plan["skill_dirs_to_move"]))
        # nothing was changed on disk
        self.assertTrue(os.path.isdir(os.path.join(self.home, ".claude", "skills", "alpha")))
        with open(os.path.join(self.home, ".claude", "settings.json")) as fh:
            self.assertTrue(json.load(fh)["enabledPlugins"]["myplug@mkt"])

    def test_apply_moves_not_deletes_and_backs_up(self):
        settings_path = os.path.join(self.home, ".claude", "settings.json")
        with open(settings_path) as fh:
            before = fh.read()
        text = self._run("--apply")
        self.assertIn("backup:", text)
        claude_dir = os.path.join(self.home, ".claude")
        backups = [f for f in os.listdir(claude_dir) if f.startswith("settings.json.context-diet-backup-")]
        self.assertEqual(len(backups), 1)
        with open(os.path.join(claude_dir, backups[0])) as fh:
            self.assertEqual(fh.read(), before)
        with open(settings_path) as fh:
            after = json.load(fh)
        self.assertFalse(after["enabledPlugins"]["myplug@mkt"])
        self.assertEqual(after["model"], "opus")  # unrelated keys preserved
        # moved, not deleted
        for name in ("alpha", "gamma"):
            self.assertFalse(os.path.exists(os.path.join(claude_dir, "skills", name)))
            dst = os.path.join(claude_dir, "skills-disabled", name, "SKILL.md")
            self.assertTrue(os.path.isfile(dst), dst)
        self.assertTrue(os.path.isfile(os.path.join(claude_dir, "skills", "beta", "SKILL.md")))
        # plugin skill files untouched on disk (only the flag flipped)
        self.assertTrue(os.path.isfile(os.path.join(claude_dir, "plugins", "cache", "mkt", "myplug", "1.0.0", "skills", "p-one", "SKILL.md")))
        # second run: plugin now disabled -> its skills are no longer counted
        data = self._json()
        self.assertEqual(data["summary"]["skills_total"], 1)
        self.assertEqual(data["skills"][0]["name"], "beta")
        self.assertFalse(data["plugins"][0]["enabled"])

    def test_max_lines_cap_is_reported(self):
        data = self._json("--max-lines", "2")
        self.assertTrue(data["scan"]["truncated"])
        self.assertLessEqual(data["scan"]["lines_read"], 3)

    def test_project_skill_and_mcp_json(self):
        _write(os.path.join(self.project, ".claude", "skills", "projskill", "SKILL.md"), _skill_md("projskill", "x" * 40))
        _write(os.path.join(self.project, ".mcp.json"), json.dumps({"mcpServers": {"projsrv": {"command": "npx", "args": ["-y", "thing", "--key=abc"]}}}))
        data = self._json()
        self.assertEqual(data["summary"]["skills_total"], 6)
        self.assertIn("project", [s["source"] for s in data["skills"]])
        mcp = {m["name"]: m for m in data["mcp_servers"]}
        self.assertEqual(mcp["projsrv"]["scope"], "project-file")
        self.assertEqual(mcp["projsrv"]["transport"], "stdio")
        self.assertNotIn("--key=abc", json.dumps(data))

    def test_frontmatter_block_scalar(self):
        fm = cd.parse_frontmatter("---\nname: z\ndescription: >\n  line one\n  line two\nother: 'q'\n---\nbody")
        self.assertEqual(fm["name"], "z")
        self.assertEqual(fm["description"], "line one\nline two")
        self.assertEqual(fm["other"], "q")
        self.assertEqual(cd.parse_frontmatter("no frontmatter"), {})

    def test_real_home_untouched(self):
        self.assertNotEqual(self.home, self.real_home)
        self.assertFalse(self.home.startswith(self.real_home + os.sep + ".claude"))


if __name__ == "__main__":
    unittest.main()
