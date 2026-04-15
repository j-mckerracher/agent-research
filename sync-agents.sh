#!/usr/bin/env bash
# sync-agents.sh — Propagate golden-copy agents to all consuming repositories.
#
# Usage:
#   ./sync-agents.sh                  # sync all repos listed in sync-agents.conf
#   ./sync-agents.sh --dry-run        # preview changes and show drift, no writes
#   ./sync-agents.sh --install-hooks  # also install pre-commit hooks in each repo
#
# Agent source mapping:
#   consuming repo .github/agents  <-  agents/          (this repo)
#   consuming repo .claude/agents  <-  .claude/agents/  (this repo)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONF_FILE="$SCRIPT_DIR/sync-agents.conf"
GOLDEN_GITHUB="$SCRIPT_DIR/agents"
GOLDEN_CLAUDE="$SCRIPT_DIR/.claude/agents"
HOOK_SOURCE="$SCRIPT_DIR/hooks/pre-commit-agent-guard"

DRY_RUN=false
INSTALL_HOOKS=false
SYNCED=0
SKIPPED=0
DRIFT_FOUND=0

for arg in "$@"; do
  case "$arg" in
    --dry-run)       DRY_RUN=true ;;
    --install-hooks) INSTALL_HOOKS=true ;;
    *) echo "Unknown argument: $arg"; exit 1 ;;
  esac
done

if [[ "$DRY_RUN" == "true" ]]; then
  echo "DRY RUN — no files will be written."
  echo ""
fi

while IFS='|' read -r repo_raw agent_dir_raw base_branch_raw || [[ -n "$repo_raw" ]]; do
  # strip whitespace and skip blanks/comments
  repo="${repo_raw//[[:space:]]/}"
  agent_dir="${agent_dir_raw//[[:space:]]/}"
  base_branch="${base_branch_raw//[[:space:]]/}"
  [[ -z "$repo" || "$repo" =~ ^# ]] && continue

  echo "── $repo / $agent_dir ──"

  if [[ ! -d "$repo" ]]; then
    echo "  WARN: repo directory not found, skipping."
    ((SKIPPED++))
    continue
  fi

  # Choose golden source based on target agent dir
  if [[ "$agent_dir" == ".claude/agents" ]]; then
    golden="$GOLDEN_CLAUDE"
  else
    golden="$GOLDEN_GITHUB"
  fi

  target="$repo/$agent_dir"

  # Drift detection
  if [[ -d "$target" ]]; then
    drift_output=$(diff -r "$golden" "$target" 2>/dev/null || true)
    if [[ -n "$drift_output" ]]; then
      echo "  DRIFT detected:"
      echo "$drift_output" | sed 's/^/    /'
      ((DRIFT_FOUND++))
    else
      echo "  No drift — already in sync."
    fi
  else
    echo "  Target dir does not exist yet, will create."
  fi

  if [[ "$DRY_RUN" == "true" ]]; then
    echo "  DRY RUN: would copy $golden -> $target"
    continue
  fi

  mkdir -p "$target"
  # Additive sync: copy all golden files into target (preserves repo-specific extras)
  cp -r "$golden"/. "$target/"
  echo "  Synced."
  ((SYNCED++))

  # Optionally install the pre-commit hook
  if [[ "$INSTALL_HOOKS" == "true" ]]; then
    if [[ -f "$HOOK_SOURCE" ]]; then
      hook_dest="$repo/.git/hooks/pre-commit"
      if [[ -f "$hook_dest" ]]; then
        # Append guard to existing hook if not already present
        if ! grep -q "agent-guard" "$hook_dest"; then
          cat "$HOOK_SOURCE" >> "$hook_dest"
          echo "  Hook appended to existing pre-commit."
        else
          echo "  Hook already present, skipped."
        fi
      else
        cp "$HOOK_SOURCE" "$hook_dest"
        chmod +x "$hook_dest"
        echo "  Hook installed."
      fi
    else
      echo "  WARN: $HOOK_SOURCE not found, skipping hook install."
    fi
  fi

done < "$CONF_FILE"

echo ""
echo "Done. Synced: $SYNCED | Skipped: $SKIPPED | Repos with drift: $DRIFT_FOUND"
if [[ "$DRY_RUN" == "true" ]]; then
  echo "(Dry run — no changes were made)"
fi
