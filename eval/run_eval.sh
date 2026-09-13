#!/usr/bin/env bash
# Runs context-diet against the bundled fixture home and prints before/after
# estimated tokens per turn. The fixture is copied to a temp dir first so the
# checked-in fixture is never modified.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CLI="$HERE/../context_diet.py"
WORK="$(mktemp -d -t context-diet-eval.XXXXXX)"
trap 'rm -rf "$WORK"' EXIT
cp -R "$HERE/fixture_home" "$WORK/home"
HOME_DIR="$WORK/home"
# The fixture stores plugin install paths relative to itself; resolve them.
python3 - "$HOME_DIR" <<'PY'
import json, sys, os
home = sys.argv[1]
p = os.path.join(home, ".claude", "plugins", "installed_plugins.json")
d = json.load(open(p))
for entries in d["plugins"].values():
    for e in entries:
        e["installPath"] = e["installPath"].replace("__FIXTURE__", home)
json.dump(d, open(p, "w"), indent=2)
PY
summary() {
  python3 "$CLI" --home "$HOME_DIR" --project "$HOME_DIR/project" --days 14 --json | python3 -c '
import json, sys
s = json.load(sys.stdin)["summary"]
print("skills=%d tokens/turn~%d unused=%d unused_tokens~%d mcp_unused=%d/%d" % (
    s["skills_total"], s["tokens_per_turn_est_total"], s["skills_unused"],
    s["tokens_per_turn_est_unused"], s["mcp_servers_unused"], s["mcp_servers_total"]))'
}

echo "== BEFORE =="
python3 "$CLI" --home "$HOME_DIR" --project "$HOME_DIR/project" --days 14 --disable-list "$WORK/plan.json"
BEFORE="$(summary)"
echo
echo "== APPLY (on the temp copy) =="
python3 "$CLI" --home "$HOME_DIR" --project "$HOME_DIR/project" --days 14 --apply | sed -n '/APPLYING/,$p'
echo
echo "== AFTER =="
AFTER="$(summary)"
echo "before: $BEFORE"
echo "after:  $AFTER"
echo "backup files: $(ls "$HOME_DIR/.claude" | grep -c 'settings.json.context-diet-backup' || true)"
echo "moved dirs:   $(ls "$HOME_DIR/.claude/skills-disabled" 2>/dev/null | tr '\n' ' ')"
if grep -qE 'secret=|token=|key=' "$WORK/plan.json"; then echo "SECRET LEAK in plan.json" >&2; exit 1; fi
echo "no secrets in plan.json: ok"
