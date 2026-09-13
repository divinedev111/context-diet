#!/usr/bin/env python3
"""context-diet: find which Claude Code skills / plugins / MCP servers cost context
every turn but are never used, and generate a safe disable-list.

Dependency-free. Python 3.9+.

    python3 context_diet.py [--days 14] [--json] [--disable-list plan.json] [--apply]

Nothing is changed unless --apply is passed. --apply never deletes: it backs up
settings.json with a timestamp and moves skill directories into skills-disabled/.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import glob
import io
import json
import math
import os
import re
import shutil
import sys
from typing import Dict, Iterable, List, Optional, Tuple
from urllib.parse import urlsplit

__version__ = "0.1.0"

# Any line in a transcript that carries this marker was produced by context-diet
# itself (its report echoed back as a tool result). Those lines mention every
# skill name and must never count as usage.
REPORT_MARKER = "CONTEXT_DIET_REPORT"

# The Claude Code system reminder that lists every available skill is stored in
# transcripts too. It contains descriptions that may quote "/name" literally, so
# that block is stripped from a line before usage matching.
SKILL_LISTING_MARKER = "skills are available for use with the Skill tool"
SKILL_LISTING_END = "</system-reminder>"

CHARS_PER_TOKEN = 4

_SENSITIVE_QUERY = re.compile(r"(secret|token|key|password|passwd|auth|sig|signature)=", re.I)
_LONG_HEX = re.compile(r"[0-9a-fA-F]{16,}")
_TIMESTAMP = re.compile(r'"timestamp"\s*:\s*"?(\d{4}-\d{2}-\d{2}T[0-9:.]+Z?|\d{10,13})')


# ----------------------------------------------------------------------------
# Small helpers
# ----------------------------------------------------------------------------

def _read_json(path: str) -> Optional[dict]:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def est_tokens(chars: int) -> int:
    return int(math.ceil(chars / CHARS_PER_TOKEN)) if chars > 0 else 0


def parse_frontmatter(text: str) -> Dict[str, str]:
    """Minimal YAML-ish frontmatter parser: top-level `key: value` pairs, with
    support for block scalars (`>` / `|`) and indented continuation lines.
    Good enough for SKILL.md files; not a YAML implementation."""
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}
    out: Dict[str, str] = {}
    key: Optional[str] = None
    buf: List[str] = []
    block = False

    def flush() -> None:
        nonlocal key, buf, block
        if key is not None:
            joined = "\n".join(buf) if block else " ".join(s.strip() for s in buf if s.strip())
            out[key] = joined.strip()
        key, buf, block = None, [], False

    for raw in lines[1:]:
        if raw.strip() == "---":
            break
        m = re.match(r"^([A-Za-z0-9_-]+):\s*(.*)$", raw)
        if m and not raw.startswith((" ", "\t")):
            flush()
            key = m.group(1)
            val = m.group(2).strip()
            if val in (">", "|", ">-", "|-"):
                block = True
                buf = []
            else:
                buf = [_strip_quotes(val)]
        elif key is not None:
            buf.append(raw.strip() if not block else raw.strip())
    flush()
    return out


def _strip_quotes(s: str) -> str:
    if len(s) >= 2 and s[0] == s[-1] and s[0] in "\"'":
        return s[1:-1]
    return s


def redact_url(url: str) -> Tuple[str, bool]:
    """Return (display, was_sensitive). Only scheme://host is ever shown."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return "[unparseable url]", True
    host = parts.hostname or ""
    scheme = parts.scheme or "http"
    sensitive = bool(_SENSITIVE_QUERY.search(url) or _LONG_HEX.search(url) or parts.username or parts.password)
    display = f"{scheme}://{host}" if host else "[no host]"
    if sensitive:
        display += " [redacted]"
    return display, sensitive


def _parse_ts(raw: str) -> Optional[_dt.datetime]:
    try:
        if raw.isdigit():
            n = int(raw)
            if n > 10**11:  # milliseconds
                n //= 1000
            return _dt.datetime.fromtimestamp(n, tz=_dt.timezone.utc)
        s = raw.rstrip("Z")
        # trim sub-second precision beyond microseconds
        if "." in s:
            head, frac = s.split(".", 1)
            s = f"{head}.{frac[:6]}"
        return _dt.datetime.fromisoformat(s).replace(tzinfo=_dt.timezone.utc)
    except (ValueError, OverflowError, OSError):
        return None


# ----------------------------------------------------------------------------
# Discovery
# ----------------------------------------------------------------------------

class Skill:
    def __init__(self, name: str, source: str, path: str, invoke_name: str,
                 description: str, file_chars: int, plugin_key: Optional[str] = None):
        self.name = name
        self.source = source              # user | project | plugin:<name>
        self.path = path                  # directory containing SKILL.md
        self.invoke_name = invoke_name    # name or plugin:name
        self.description = description
        self.desc_chars = len(description)
        self.tokens_per_turn = est_tokens(self.desc_chars + len(invoke_name) + 4)
        self.file_chars = file_chars
        self.file_tokens = est_tokens(file_chars)
        self.plugin_key = plugin_key      # e.g. vercel@claude-plugins-official
        self.uses_total = 0
        self.uses_in_window = 0
        self.last_used: Optional[_dt.datetime] = None

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "invoke_name": self.invoke_name,
            "source": self.source,
            "path": self.path,
            "plugin_key": self.plugin_key,
            "description_chars": self.desc_chars,
            "tokens_per_turn_est": self.tokens_per_turn,
            "skill_md_chars": self.file_chars,
            "skill_md_tokens_est": self.file_tokens,
            "uses_total": self.uses_total,
            "uses_in_window": self.uses_in_window,
            "last_used": self.last_used.isoformat() if self.last_used else None,
        }


class McpServer:
    def __init__(self, name: str, scope: str, config: dict):
        self.name = name
        self.scope = scope  # user | project:<path> | project-file
        self.transport = config.get("type") or ("stdio" if config.get("command") else "http")
        self.command = config.get("command")
        self.display = None
        self.redacted = False
        url = config.get("url")
        if isinstance(url, str):
            self.display, self.redacted = redact_url(url)
        self.uses_total = 0
        self.uses_in_window = 0
        self.last_used: Optional[_dt.datetime] = None

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "scope": self.scope,
            "transport": self.transport,
            "command": self.command,
            "url_host": self.display,
            "url_redacted": self.redacted,
            "uses_total": self.uses_total,
            "uses_in_window": self.uses_in_window,
            "last_used": self.last_used.isoformat() if self.last_used else None,
        }


def _load_skill(skill_md: str, source: str, invoke_prefix: str = "", plugin_key: Optional[str] = None) -> Optional[Skill]:
    try:
        with open(skill_md, "r", encoding="utf-8", errors="replace") as fh:
            text = fh.read()
    except OSError:
        return None
    fm = parse_frontmatter(text)
    dirname = os.path.basename(os.path.dirname(skill_md))
    name = fm.get("name") or dirname
    return Skill(
        name=name,
        source=source,
        path=os.path.dirname(skill_md),
        invoke_name=f"{invoke_prefix}{name}",
        description=fm.get("description", ""),
        file_chars=len(text),
        plugin_key=plugin_key,
    )


def discover_skills(home: str, project: str) -> Tuple[List[Skill], List[dict]]:
    skills: List[Skill] = []
    plugins_info: List[dict] = []

    for skill_md in sorted(glob.glob(os.path.join(home, ".claude", "skills", "*", "SKILL.md"))):
        s = _load_skill(skill_md, "user")
        if s:
            skills.append(s)

    for skill_md in sorted(glob.glob(os.path.join(project, ".claude", "skills", "*", "SKILL.md"))):
        s = _load_skill(skill_md, "project")
        if s:
            skills.append(s)

    settings = _read_json(os.path.join(home, ".claude", "settings.json")) or {}
    enabled_map = settings.get("enabledPlugins") if isinstance(settings.get("enabledPlugins"), dict) else {}

    installed = _read_json(os.path.join(home, ".claude", "plugins", "installed_plugins.json")) or {}
    plugins = installed.get("plugins", installed)
    if isinstance(plugins, dict):
        for key, entries in sorted(plugins.items()):
            plugin_name = key.split("@", 1)[0]
            if isinstance(entries, dict):
                entries = [entries]
            if not isinstance(entries, list):
                continue
            install_path = None
            for e in entries:
                if isinstance(e, dict) and isinstance(e.get("installPath"), str) and os.path.isdir(e["installPath"]):
                    install_path = e["installPath"]
            enabled = enabled_map.get(key, True) is not False
            found = 0
            if install_path:
                for skill_md in sorted(glob.glob(os.path.join(install_path, "skills", "*", "SKILL.md"))):
                    s = _load_skill(skill_md, f"plugin:{plugin_name}", f"{plugin_name}:", plugin_key=key)
                    if s:
                        found += 1
                        if enabled:
                            skills.append(s)
            plugins_info.append({
                "key": key,
                "name": plugin_name,
                "install_path": install_path,
                "enabled": enabled,
                "skills_found": found,
            })
    return skills, plugins_info


def discover_mcp(home: str, project: str) -> List[McpServer]:
    servers: List[McpServer] = []
    cfg = _read_json(os.path.join(home, ".claude.json")) or {}
    for name, c in sorted((cfg.get("mcpServers") or {}).items()):
        if isinstance(c, dict):
            servers.append(McpServer(name, "user", c))
    projects = cfg.get("projects") or {}
    if isinstance(projects, dict):
        for ppath, pcfg in sorted(projects.items()):
            if not isinstance(pcfg, dict):
                continue
            for name, c in sorted((pcfg.get("mcpServers") or {}).items()):
                if isinstance(c, dict):
                    servers.append(McpServer(name, f"project:{ppath}", c))
    pfile = _read_json(os.path.join(project, ".mcp.json")) or {}
    for name, c in sorted((pfile.get("mcpServers") or {}).items()):
        if isinstance(c, dict):
            servers.append(McpServer(name, "project-file", c))
    return servers


# ----------------------------------------------------------------------------
# Usage scanning
# ----------------------------------------------------------------------------

def _history_files(home: str) -> List[str]:
    files: List[str] = []
    hist = os.path.join(home, ".claude", "history.jsonl")
    if os.path.isfile(hist):
        files.append(hist)
    proj = sorted(
        glob.glob(os.path.join(home, ".claude", "projects", "**", "*.jsonl"), recursive=True),
        key=lambda p: os.path.getmtime(p) if os.path.exists(p) else 0,
        reverse=True,  # newest first so the line cap keeps recent history
    )
    files.extend(proj)
    return files


def _strip_listing(line: str) -> str:
    i = line.find(SKILL_LISTING_MARKER)
    while i >= 0:
        j = line.find(SKILL_LISTING_END, i)
        line = line[:i] + (line[j + len(SKILL_LISTING_END):] if j >= 0 else "")
        i = line.find(SKILL_LISTING_MARKER)
    return line


def _build_skill_regex(skills: Iterable[Skill]) -> Optional[re.Pattern]:
    names = sorted({s.invoke_name for s in skills} | {s.name for s in skills}, key=len, reverse=True)
    if not names:
        return None
    alt = "|".join(re.escape(n) for n in names)
    # Accepted invocation shapes:
    #   /name                (not inside a filesystem path)
    #   "skill":"name"       (Skill tool input in transcripts)
    #   Skill(name)          (UI rendering)
    #   <command-name>/name  (slash-command records)
    # `name` may itself be plugin-namespaced, e.g. vercel:nextjs.
    return re.compile(
        r'(?:(?<![\w./:-])/|"skill"\s*:\s*\\?"|Skill\(\s*\\?"?|<command-name>/?)'
        r"(" + alt + r")(?![\w-])"
    )


def scan_usage(home: str, skills: List[Skill], servers: List[McpServer],
               days: int, max_lines: int) -> dict:
    by_invoke: Dict[str, List[Skill]] = {}
    for s in skills:
        by_invoke.setdefault(s.invoke_name, []).append(s)
        by_invoke.setdefault(s.name, []).append(s)
    by_server: Dict[str, List[McpServer]] = {}
    for m in servers:
        by_server.setdefault(m.name, []).append(m)

    skill_re = _build_skill_regex(skills)
    mcp_re = re.compile(r"mcp__([A-Za-z0-9_-]+?)__[A-Za-z0-9_]") if servers else None

    cutoff = _dt.datetime.now(tz=_dt.timezone.utc) - _dt.timedelta(days=days)
    lines_read = 0
    files_read = 0
    truncated = False

    def bump(obj, ts: Optional[_dt.datetime]) -> None:
        obj.uses_total += 1
        if ts is not None:
            if ts >= cutoff:
                obj.uses_in_window += 1
            if obj.last_used is None or ts > obj.last_used:
                obj.last_used = ts

    for path in _history_files(home):
        if truncated:
            break
        try:
            fh = open(path, "r", encoding="utf-8", errors="replace")
        except OSError:
            continue
        files_read += 1
        with fh:
            for line in fh:
                lines_read += 1
                if lines_read > max_lines:
                    truncated = True
                    break
                if REPORT_MARKER in line:
                    continue
                if SKILL_LISTING_MARKER in line:
                    line = _strip_listing(line)
                hit_skills = set()
                hit_servers = set()
                if skill_re is not None:
                    for m in skill_re.finditer(line):
                        hit_skills.add(m.group(1))
                if mcp_re is not None:
                    for m in mcp_re.finditer(line):
                        if m.group(1) in by_server:
                            hit_servers.add(m.group(1))
                if not hit_skills and not hit_servers:
                    continue
                tm = _TIMESTAMP.search(line)
                ts = _parse_ts(tm.group(1)) if tm else None
                seen = set()
                for key in hit_skills:
                    for s in by_invoke.get(key, []):
                        if id(s) not in seen:
                            seen.add(id(s))
                            bump(s, ts)
                for key in hit_servers:
                    for srv in by_server[key]:
                        bump(srv, ts)
    return {"lines_read": lines_read, "files_read": files_read, "truncated": truncated,
            "max_lines": max_lines, "cutoff": cutoff.isoformat()}


# ----------------------------------------------------------------------------
# Plan (disable-list) + apply
# ----------------------------------------------------------------------------

def build_plan(home: str, project: str, skills: List[Skill], plugins_info: List[dict],
               servers: List[McpServer], days: int) -> dict:
    unused = [s for s in skills if s.uses_in_window == 0]
    plugin_skills: Dict[str, List[Skill]] = {}
    for s in skills:
        if s.plugin_key:
            plugin_skills.setdefault(s.plugin_key, []).append(s)

    plugins_to_disable = []
    for key, group in sorted(plugin_skills.items()):
        if all(s.uses_in_window == 0 for s in group):
            plugins_to_disable.append({
                "key": key,
                "skills": [s.name for s in group],
                "tokens_per_turn_est": sum(s.tokens_per_turn for s in group),
                "action": f'set enabledPlugins["{key}"] = false in {os.path.join(home, ".claude", "settings.json")}',
            })
    plugins_kept = [key for key, group in plugin_skills.items() if any(s.uses_in_window > 0 for s in group)]
    plugins_no_skills = [p["key"] for p in plugins_info if p["enabled"] and p["skills_found"] == 0]

    moves = []
    for s in unused:
        if s.plugin_key:
            continue
        root = os.path.join(home, ".claude") if s.source == "user" else os.path.join(project, ".claude")
        dest = os.path.join(root, "skills-disabled", os.path.basename(s.path))
        moves.append({
            "name": s.name,
            "source": s.source,
            "from": s.path,
            "to": dest,
            "tokens_per_turn_est": s.tokens_per_turn,
            "action": "move directory (never delete)",
        })

    unused_servers = [m.to_dict() for m in servers if m.uses_in_window == 0]

    saved = sum(p["tokens_per_turn_est"] for p in plugins_to_disable) + sum(m["tokens_per_turn_est"] for m in moves)
    return {
        "report_marker": REPORT_MARKER,
        "generated_at": _dt.datetime.now(tz=_dt.timezone.utc).isoformat(),
        "window_days": days,
        "settings_path": os.path.join(home, ".claude", "settings.json"),
        "plugins_to_disable": plugins_to_disable,
        "plugins_kept_because_used": sorted(plugins_kept),
        "plugins_skipped_no_skills_found": sorted(plugins_no_skills),
        "skill_dirs_to_move": moves,
        "mcp_servers_unused_informational": unused_servers,
        "mcp_note": "MCP servers are not changed by --apply. To remove one manually: `claude mcp remove <name>` (backs nothing up; re-add later with `claude mcp add`).",
        "estimated_tokens_per_turn_saved": saved,
        "applied": False,
    }


def apply_plan(plan: dict, out=sys.stdout) -> dict:
    """Apply a plan: back up settings.json, flip enabledPlugins, move skill dirs.
    Never deletes anything."""
    results = {"backup": None, "plugins_disabled": [], "moved": [], "skipped": []}
    settings_path = plan["settings_path"]
    if plan["plugins_to_disable"]:
        settings = _read_json(settings_path)
        if settings is None:
            if os.path.exists(settings_path):
                results["skipped"].append(f"settings.json unreadable, plugins not touched: {settings_path}")
                settings = None
            else:
                settings = {}
        if settings is not None:
            os.makedirs(os.path.dirname(settings_path), exist_ok=True)
            stamp = _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
            if os.path.exists(settings_path):
                backup = f"{settings_path}.context-diet-backup-{stamp}"
                shutil.copy2(settings_path, backup)
                results["backup"] = backup
                print(f"backup: {backup}", file=out)
            enabled = settings.setdefault("enabledPlugins", {})
            if not isinstance(enabled, dict):
                enabled = settings["enabledPlugins"] = {}
            for p in plan["plugins_to_disable"]:
                enabled[p["key"]] = False
                results["plugins_disabled"].append(p["key"])
                print(f"disabled plugin: {p['key']}", file=out)
            tmp = settings_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(settings, fh, indent=2)
                fh.write("\n")
            os.replace(tmp, settings_path)
    for mv in plan["skill_dirs_to_move"]:
        src, dst = mv["from"], mv["to"]
        if not os.path.isdir(src):
            results["skipped"].append(f"missing: {src}")
            continue
        if os.path.exists(dst):
            results["skipped"].append(f"destination exists, not overwriting: {dst}")
            continue
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.move(src, dst)
        results["moved"].append({"from": src, "to": dst})
        print(f"moved: {src} -> {dst}", file=out)
    return results


# ----------------------------------------------------------------------------
# Reporting
# ----------------------------------------------------------------------------

def _fmt_ts(ts: Optional[_dt.datetime]) -> str:
    return ts.strftime("%Y-%m-%d") if ts else "never*"


def render_report(skills: List[Skill], servers: List[McpServer], plugins_info: List[dict],
                  scan: dict, days: int, plan: dict) -> str:
    buf = io.StringIO()
    w = buf.write
    w(f"context-diet v{__version__}  ({REPORT_MARKER})\n")
    w(f"window: last {days} days   transcript lines scanned: {scan['lines_read']:,} across {scan['files_read']} files")
    if scan["truncated"]:
        w(f"   [capped at --max-lines {scan['max_lines']:,}; raise it for older usage]")
    w("\n\n")

    rows = sorted(skills, key=lambda s: (-s.tokens_per_turn, s.invoke_name))
    name_w = max([len("name")] + [len(s.invoke_name) for s in rows])
    src_w = max([len("source")] + [len(s.source) for s in rows])
    w(f"{'name':<{name_w}}  {'source':<{src_w}}  {'tok/turn':>8}  {'uses':>5}  last used\n")
    w(f"{'-' * name_w}  {'-' * src_w}  {'-' * 8}  {'-' * 5}  ----------\n")
    for s in rows:
        flag = " " if s.uses_in_window else "!"
        w(f"{s.invoke_name:<{name_w}}  {s.source:<{src_w}}  {s.tokens_per_turn:>8}  {s.uses_in_window:>5}  {_fmt_ts(s.last_used)} {flag}\n")
    w("\n")

    if servers:
        w("MCP servers (tool schemas also cost context; size unknown without connecting)\n")
        n_w = max([len("name")] + [len(m.name) for m in servers])
        s_w = max([len("scope")] + [min(len(m.scope), 40) for m in servers])
        w(f"{'name':<{n_w}}  {'scope':<{s_w}}  {'uses':>5}  last used   endpoint\n")
        for m in sorted(servers, key=lambda m: (m.uses_in_window, m.name)):
            endpoint = m.display or (f"stdio: {m.command}" if m.command else "?")
            flag = " " if m.uses_in_window else "!"
            w(f"{m.name:<{n_w}}  {m.scope[:40]:<{s_w}}  {m.uses_in_window:>5}  {_fmt_ts(m.last_used)} {flag} {endpoint}\n")
        w("\n")

    disabled_plugins = [p["key"] for p in plugins_info if not p["enabled"]]
    if disabled_plugins:
        w(f"already-disabled plugins (not counted): {', '.join(disabled_plugins)}\n")
    if plan["plugins_skipped_no_skills_found"]:
        w(f"plugins with no skills found (hooks/agents/commands only; not proposed): {', '.join(plan['plugins_skipped_no_skills_found'])}\n")
    partial = []
    for key in plan["plugins_kept_because_used"]:
        group = [s for s in skills if s.plugin_key == key]
        used = [s for s in group if s.uses_in_window]
        dead = sum(s.tokens_per_turn for s in group if not s.uses_in_window)
        if dead:
            partial.append(f"{key}: {len(used)}/{len(group)} skills used, ~{dead:,} tokens/turn in unused ones")
    if partial:
        w("partially used plugins (plugins can only be disabled whole, so these are kept):\n")
        for line in partial:
            w(f"  {line}\n")

    total_tok = sum(s.tokens_per_turn for s in skills)
    unused = [s for s in skills if s.uses_in_window == 0]
    unused_tok = sum(s.tokens_per_turn for s in unused)
    unused_srv = [m for m in servers if m.uses_in_window == 0]
    w("\n")
    w(f"SUMMARY: {len(skills)} skills, ~{total_tok:,} est tokens/turn; "
      f"{len(unused)} unused in {days}d (~{unused_tok:,} tokens/turn, {100 * unused_tok // total_tok if total_tok else 0}%); "
      f"{len(unused_srv)}/{len(servers)} MCP servers unused\n")
    w(f"DISABLE-LIST would save ~{plan['estimated_tokens_per_turn_saved']:,} tokens/turn: "
      f"{len(plan['plugins_to_disable'])} plugin(s) to disable, {len(plan['skill_dirs_to_move'])} skill dir(s) to move\n")
    w("* never = no invocation found in the scanned window/files; see README limitations\n")
    return buf.getvalue()


# ----------------------------------------------------------------------------
# Entry point
# ----------------------------------------------------------------------------

def run(argv: Optional[List[str]] = None, out=sys.stdout) -> int:
    ap = argparse.ArgumentParser(prog="context-diet", description=__doc__.split("\n\n")[0])
    ap.add_argument("--home", default=os.path.expanduser("~"), help="home directory (contains .claude/ and .claude.json)")
    ap.add_argument("--project", default=os.getcwd(), help="project directory (contains .claude/skills and .mcp.json)")
    ap.add_argument("--days", type=int, default=14, help="usage window in days (default 14)")
    ap.add_argument("--max-lines", type=int, default=500_000, help="cap on transcript lines scanned (default 500000)")
    ap.add_argument("--json", action="store_true", help="print JSON instead of the table")
    ap.add_argument("--disable-list", metavar="PATH", help="write the proposed disable-list (JSON) to PATH")
    ap.add_argument("--apply", action="store_true", help="APPLY the disable-list: back up settings.json, set plugins false, move skill dirs (never deletes)")
    ap.add_argument("--version", action="version", version=__version__)
    args = ap.parse_args(argv)

    home = os.path.abspath(os.path.expanduser(args.home))
    project = os.path.abspath(os.path.expanduser(args.project))

    skills, plugins_info = discover_skills(home, project)
    servers = discover_mcp(home, project)
    scan = scan_usage(home, skills, servers, args.days, args.max_lines)
    plan = build_plan(home, project, skills, plugins_info, servers, args.days)

    if args.disable_list:
        with open(args.disable_list, "w", encoding="utf-8") as fh:
            json.dump(plan, fh, indent=2)
            fh.write("\n")

    if args.json:
        total_tok = sum(s.tokens_per_turn for s in skills)
        unused = [s for s in skills if s.uses_in_window == 0]
        payload = {
            "report_marker": REPORT_MARKER,
            "version": __version__,
            "home": home,
            "project": project,
            "window_days": args.days,
            "scan": scan,
            "skills": [s.to_dict() for s in skills],
            "plugins": plugins_info,
            "mcp_servers": [m.to_dict() for m in servers],
            "summary": {
                "skills_total": len(skills),
                "tokens_per_turn_est_total": total_tok,
                "skills_unused": len(unused),
                "tokens_per_turn_est_unused": sum(s.tokens_per_turn for s in unused),
                "mcp_servers_total": len(servers),
                "mcp_servers_unused": sum(1 for m in servers if m.uses_in_window == 0),
            },
            "plan": plan,
        }
        print(json.dumps(payload, indent=2), file=out)
    else:
        out.write(render_report(skills, servers, plugins_info, scan, args.days, plan))
        if args.disable_list:
            print(f"disable-list written to {args.disable_list} (not applied; pass --apply to apply)", file=out)

    if args.apply:
        print("\nAPPLYING disable-list (backup first, move never delete)...", file=out)
        res = apply_plan(plan, out=out)
        for s in res["skipped"]:
            print(f"skipped: {s}", file=out)
        print(f"done: {len(res['plugins_disabled'])} plugin(s) disabled, {len(res['moved'])} dir(s) moved"
              + (f", backup at {res['backup']}" if res["backup"] else ""), file=out)
    return 0


if __name__ == "__main__":
    sys.exit(run())
