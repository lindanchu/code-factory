#!/usr/bin/env bash
# Manual alternative to the plugin: install the software factory as a personal
# Claude Code skill in ~/.claude (or $CLAUDE_CONFIG_DIR). The command is then
# /factory instead of /code-factory:factory. Do not use both at the same time.
#
#   bash install.sh [--force]
#
# A personal skill and its agents have plain names, so a different "factory"
# skill, or other factory-*.md agent files, may already be there. So this script
# only replaces what it installed itself, unchanged: the skill folder gets a file,
# .code-factory-install, that lists what it installed with each file's checksum.
# Anything else stops it, unless you add --force.
set -euo pipefail

usage() {
  echo "usage: bash install.sh [--force]"
  echo "Installs code-factory as a personal skill in ${CLAUDE_CONFIG_DIR:-~/.claude}."
  echo "--force replaces a personal factory skill or agent files that it did not install."
}

FORCE=0
for arg in "$@"; do
  case "$arg" in
    --force) FORCE=1 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "error: unknown option: $arg" >&2; usage >&2; exit 2 ;;
  esac
done

SRC="$(cd "$(dirname "$0")" && pwd)/plugins/code-factory"
DEST="${CLAUDE_CONFIG_DIR:-$HOME/.claude}"
SKILL="$DEST/skills/factory"
AGENTS="$DEST/agents"
MARK="$SKILL/.code-factory-install"   # its first line is exactly "code-factory"

if [ ! -f "$SRC/skills/factory/fx" ]; then
  echo "error: $SRC/skills/factory/fx is missing; run install.sh from a code-factory checkout." >&2
  exit 1
fi

# A file's fingerprint: POSIX cksum (CRC and size), the same on macOS and Linux.
fp() { cksum < "$1" | awk '{ print $1 "-" $2 }'; }

# What the last install recorded for a path under $DEST ("" when nothing).
recorded() { awk -v k="$1" 'NR > 1 && $1 == k { print $2; exit }' "$MARK" 2>/dev/null || true; }

# Whose is the skill folder? ours: this script installed it and its fx is unchanged.
state=none
if [ -L "$SKILL" ]; then
  state=link
elif [ -e "$SKILL" ]; then
  if [ -f "$MARK" ] && [ "$(head -n 1 "$MARK")" = "code-factory" ]; then
    if [ -f "$SKILL/fx" ] && [ "$(recorded skills/factory/fx)" = "$(fp "$SKILL/fx")" ]; then
      state=ours
    else
      state=changed
    fi
  elif [ -f "$SKILL/fx" ]; then
    state=other
  else
    state=notfactory
  fi
fi

# Agent files this install would replace: each must be the very same file, or ours and unchanged since the last install.
foreign=""
for f in "$SRC"/agents/factory-*.md; do
  name="${f##*/}"
  dest="$AGENTS/$name"
  if [ ! -e "$dest" ] && [ ! -L "$dest" ]; then
    continue
  fi
  if [ -f "$dest" ] && [ ! -L "$dest" ]; then
    if [ "$(fp "$dest")" = "$(fp "$f")" ]; then
      continue
    fi
    if [ "$state" = ours ] && [ "$(recorded "agents/$name")" = "$(fp "$dest")" ]; then
      continue
    fi
  fi
  foreign="$foreign $name"
done

# Anything found here that is not ours stops the install, unless --force.
if [ "$FORCE" = 1 ]; then tag="note:"; else tag="error:"; fi
problem=0
case "$state" in
  link)
    echo "$tag $SKILL is a link to $(readlink "$SKILL"); install.sh does not write through it." >&2
    problem=1 ;;
  other)
    echo "$tag $SKILL holds a different 'factory' skill, which this install.sh did not install." >&2
    problem=1 ;;
  changed)
    echo "$tag $SKILL was installed by code-factory's install.sh, but its fx is missing or has changed" \
      "since (another install may have copied over it)." >&2
    problem=1 ;;
  notfactory)
    echo "$tag $SKILL already exists and is not a factory skill." >&2
    problem=1 ;;
esac
if [ -n "$foreign" ]; then
  echo "$tag agent files in $AGENTS that code-factory's install.sh did not install, or that changed since:$foreign" >&2
  problem=1
fi
if [ "$problem" = 1 ]; then
  if [ "$FORCE" != 1 ]; then
    echo "Nothing was changed. To keep them, install code-factory as a plugin instead (see Install in" >&2
    echo "README.md): a plugin's skill and agents carry its name, so nothing clashes. To replace them:" >&2
    echo "  bash install.sh --force" >&2
    exit 1
  fi
  echo "--force: replacing them." >&2
fi

# Our agent files that this version no longer ships (read from the last install's list before it is rewritten).
old_agents=""
if [ "$state" = ours ]; then
  old_agents="$(awk 'NR > 1 && $1 ~ /^agents\// { print substr($1, 8) }' "$MARK")"
fi

if [ -L "$SKILL" ] || { [ -e "$SKILL" ] && [ ! -d "$SKILL" ]; }; then
  rm -f "$SKILL"   # the link or file itself, never what a link points to
fi
mkdir -p "$SKILL" "$AGENTS"
cp -R "$SRC/skills/factory/." "$SKILL/"
for f in "$SRC"/agents/factory-*.md; do
  rm -f "$AGENTS/${f##*/}"   # a link is replaced, never written through
  cp "$f" "$AGENTS/"
done
chmod +x "$SKILL/fx" "$SKILL/templates/checks.sh"

for name in $old_agents; do
  case "$name" in */*) continue ;; factory-*.md) ;; *) continue ;; esac
  dest="$AGENTS/$name"
  if [ ! -e "$SRC/agents/$name" ] && [ -f "$dest" ] && [ ! -L "$dest" ] \
      && [ "$(recorded "agents/$name")" = "$(fp "$dest")" ]; then
    rm -f "$dest"
    echo "removed $dest: no longer part of code-factory"
  fi
done

{
  echo "code-factory"
  echo "# Written by code-factory's install.sh: what it installed, with each file's cksum. Do not edit."
  echo "version $(cat "$SKILL/VERSION")"
  echo "skills/factory/fx $(fp "$SKILL/fx")"
  for f in "$SRC"/agents/factory-*.md; do
    echo "agents/${f##*/} $(fp "$AGENTS/${f##*/}")"
  done
} > "$MARK.tmp"
mv -f "$MARK.tmp" "$MARK"

extra=""
for f in "$AGENTS"/factory-*.md; do
  if [ -e "$f" ] && [ ! -e "$SRC/agents/${f##*/}" ]; then
    extra="$extra ${f##*/}"
  fi
done
if [ -n "$extra" ]; then
  echo "note: $AGENTS also holds agent files code-factory does not use; they were left as they are:$extra"
fi

command -v python3 >/dev/null || echo "warning: python3 not found; on macOS run: xcode-select --install" >&2

echo "code-factory $(cat "$SKILL/VERSION") installed in $DEST"
echo "Next, in Claude Code inside your factory repository:  /factory init   then   /factory PROJ-123"
