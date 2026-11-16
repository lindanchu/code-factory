#!/usr/bin/env bash
# Manual alternative to the plugin: install the software factory as personal Claude Code
# skills in ~/.claude (or $CLAUDE_CONFIG_DIR). The commands are then /auto-factory-code and
# /auto-factory-review instead of the plugin's /auto-factory:code and /auto-factory:review.
# Do not use both ways at the same time.
#
#   bash install.sh
set -euo pipefail

SRC="$(cd "$(dirname "$0")" && pwd)/plugins/auto-factory"
DEST="${CLAUDE_CONFIG_DIR:-$HOME/.claude}"
CODE="$DEST/skills/auto-factory-code"
REVIEW="$DEST/skills/auto-factory-review"
FORCE="${1:-}"

# A folder that is already there is replaced only when this script put it there.
check() {  # <folder> <skill name>
  if [ -d "$1" ] && [ "$FORCE" != "--force" ] && ! grep -qx "name: $2" "$1/SKILL.md" 2>/dev/null; then
    echo "error: $1 already exists and is not this factory. Rename it or run with --force." >&2
    exit 1
  fi
}
check "$CODE" auto-factory-code
check "$REVIEW" auto-factory-review

mkdir -p "$CODE" "$REVIEW" "$DEST/agents"
cp -R "$SRC/skills/code/." "$CODE/"
cp -R "$SRC/skills/review/." "$REVIEW/"
find "$CODE" -name __pycache__ -type d -prune -exec rm -rf {} +

# A personal skill's command is its name: each gets the name of its folder.
name_skill() {  # <folder> <name in the plugin> <name here>
  sed -i.bak "2s/^name: $2\$/name: $3/" "$1/SKILL.md" && rm -f "$1/SKILL.md.bak"
  grep -qx "name: $3" "$1/SKILL.md" || { echo "error: could not name $1/SKILL.md" >&2; exit 1; }
}
name_skill "$CODE" code auto-factory-code
name_skill "$REVIEW" review auto-factory-review

cp "$SRC"/agents/factory-*.md "$DEST/agents/"
chmod +x "$CODE/fx" "$CODE/templates/checks.sh"

# Older versions installed the command /factory: such a copy would still answer to it.
for old in factory:fx; do
  dir="$DEST/skills/${old%%:*}"
  if [ -f "$dir/${old##*:}" ]; then
    rm -rf "$dir"
    echo "removed the older copy in $dir (its command is now one of the two below)"
  fi
done

command -v python3 >/dev/null || echo "warning: python3 not found; on macOS run: xcode-select --install" >&2

echo "Software factory $(cat "$CODE/VERSION") installed in $DEST"
echo "Next, in Claude Code inside a factory repository:  /auto-factory-code init   then   /auto-factory-code PROJ-123"
echo "A colleague's merge request, in that repository:  /auto-factory-review <its link, or PROJ-123>"
