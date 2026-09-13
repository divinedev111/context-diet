# context-diet

Tells a Claude Code user which installed **skills, plugins and MCP servers cost context every turn but are never used**, and generates a safe, reversible disable-list.

Every skill's frontmatter `description` is injected into the model's context on every turn so Claude knows when to trigger it. Twenty plugin skills you never invoke can quietly eat 2-3k tokens of every request. `context-diet` finds them.

Dependency-free Python 3.9+. One file. Read-only unless you say `--apply`.

## What it does

- **Discovers**
  - user skills: `~/.claude/skills/*/SKILL.md`
  - project skills: `<cwd>/.claude/skills/*/SKILL.md`
  - plugin skills: every entry in `~/.claude/plugins/installed_plugins.json` and its `skills/*/SKILL.md` (respecting `enabledPlugins` in `~/.claude/settings.json`)
  - MCP servers: `~/.claude.json` (`mcpServers` and per-project `projects.<path>.mcpServers`) and `<cwd>/.mcp.json`
- **Estimates cost**: characters of the frontmatter description / 4 = tokens per turn (plus the full SKILL.md size as a secondary number, which is only paid when the skill actually triggers).
- **Detects usage**: streams `~/.claude/history.jsonl` and `~/.claude/projects/**/*.jsonl` (newest first, capped at `--max-lines`, default 500k) looking for `/<name>`, `"skill":"<name>"`, `Skill(<name>)`, `<command-name>/<name>` and plugin-namespaced `<plugin>:<name>` invocations, plus `mcp__<server>__` tool calls, recording the last-used timestamp.
- **Proposes a disable-list**: plugins whose skills are all unused → `enabledPlugins[key] = false`; unused user/project skills → move the directory to `.claude/skills-disabled/`. Unused MCP servers are listed for you but never changed.
- **Applies only on `--apply`**: makes a timestamped backup of `settings.json` first, then moves directories. It never deletes.
- **Redacts**: MCP URLs are shown as `scheme://host` only, flagged `[redacted]` when the URL carried `secret=`, `token=`, `key=`, credentials or a long hex id. `env` blocks and stdio args are never printed.

## Install

As a Claude Code skill (so you can just say "context diet" in a session):

```bash
mkdir -p ~/.claude/skills/context-diet
cp SKILL.md context_diet.py ~/.claude/skills/context-diet/
```

Or per-project: copy the same two files into `<repo>/.claude/skills/context-diet/`.

Or run it standalone with no install at all: `python3 context_diet.py`.

## Usage

```bash
python3 context_diet.py                       # report, last 14 days
python3 context_diet.py --days 30             # wider window
python3 context_diet.py --json                # machine-readable
python3 context_diet.py --disable-list plan.json   # write the proposed plan, change nothing
python3 context_diet.py --apply               # backup settings.json, flip plugins, move dirs
python3 context_diet.py --home /path --project /path   # test against another tree
```

In Claude Code, with the skill installed: say **"context diet"** or **"which skills am I not using"**. Claude runs the report and asks before applying anything.

## Sample output

```
context-diet v0.1.0  (CONTEXT_DIET_REPORT)
window: last 14 days   transcript lines scanned: 246,418 across 2695 files

name                        source             tok/turn   uses  last used
--------------------------  -----------------  --------  -----  ----------
helix                       user                    255      8  2026-09-12
vercel:microfrontends       plugin:vercel           153      0  never* !
vercel:eve                  plugin:vercel           150      0  never* !
council                     user                    137      2  2026-09-04
vercel:marketplace          plugin:vercel           115      0  never* !
...

MCP servers (tool schemas also cost context; size unknown without connecting)
name    scope   uses  last used   endpoint
figma   user      10  2026-09-12   https://mcp.figma.com
framer  user      13  2026-09-12   https://mcp.unframer.co [redacted]

partially used plugins (plugins can only be disabled whole, so these are kept):
  vercel@claude-plugins-official: 2/35 skills used, ~2,433 tokens/turn in unused ones

SUMMARY: 43 skills, ~3,370 est tokens/turn; 35 unused in 14d (~2,484 tokens/turn, 73%); 0/2 MCP servers unused
DISABLE-LIST would save ~0 tokens/turn: 0 plugin(s) to disable, 0 skill dir(s) to move
```

(That last line is honest: the 35-skill `vercel` plugin had two skills used, and plugins can only be disabled whole. The report still tells you 73% of your per-turn skill overhead is dead weight, so you can decide.)

`eval/run_eval.sh` shows a before/after on the bundled fixture.

## Limitations (read these)

- **Token numbers are estimates.** chars/4 is a rough proxy for tokenizer output; the exact per-skill overhead inside Claude Code's system prompt is not published and includes formatting we do not model. Treat the numbers as relative, not absolute.
- **Usage detection is a heuristic over local transcript files.** It looks for a handful of invocation shapes. It will miss skills Claude triggered silently in a way that does not leave those strings, and it can produce a false positive when one of those strings appears in ordinary prose. It strips the per-session skill-listing block and its own report lines so they do not count as usage.
- **Only local history is read.** Sessions from another machine, deleted transcripts, or history older than `--max-lines` are invisible. The report marks the cap when it hits it.
- **MCP servers are measured for usage only.** Their real context cost (tool schemas) requires connecting to the server, which this tool deliberately does not do. They are never modified.
- **Plugins are all-or-nothing.** If one skill in a plugin is used, the plugin is kept, even if 33 others are dead weight. The report shows that so you can weigh it.
- **Plugins with no `skills/` directory** (hooks-, agents- or commands-only) are listed but never proposed for disabling; the tool cannot see their usage.
- Not affiliated with Anthropic. Tested on Claude Code layouts as of September 2026; if the on-disk format changes, discovery may miss things silently (counts in the summary will look low).

## Undo

- `~/.claude/settings.json.context-diet-backup-<timestamp>` → copy back over `settings.json`
- `~/.claude/skills-disabled/<name>` → move back to `~/.claude/skills/<name>`

## Tests

```bash
python3 -m unittest discover -s products/context-diet/tests
```

---

Want this adapted to your repo/stack? Custom Claude Code / Codex skills, async, tested, no call — **$79**. https://github.com/divinedev111/context-diet/issues/new?template=custom-request.md
