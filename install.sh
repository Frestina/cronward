#!/usr/bin/env bash
# Links bin/cronward into ~/.local/bin, and the agent skill into the skills
# folders that are there: ~/.agents/skills (read by many agents) and
# ~/.claude/skills (Claude Code). Safe to run again.
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
dir="$HOME/.local/bin"

# Makes $2 a link to $1, unless something that is not a link is there.
link() {
  local src=$1 dest=$2 old
  if [[ -L $dest ]]; then
    old="$(readlink "$dest")"
    if [[ $old == "$src" ]]; then
      echo "$dest already links to $src"
      return 0
    fi
    echo "replacing link to $old"
  elif [[ -e $dest ]]; then
    echo "install.sh: $dest exists and is not a link, leaving it alone" >&2
    return 1
  fi
  mkdir -p "$(dirname "$dest")"
  ln -sfn "$src" "$dest"
  echo "linked $dest -> $src"
}

link "$root/bin/cronward" "$dir/cronward"

found=0
for skills in "$HOME/.agents/skills" "$HOME/.claude/skills"; do
  if [[ -d $skills ]]; then
    found=1
    link "$root/skills/cronward" "$skills/cronward" || true
  fi
done
if ((found == 0)); then
  echo "note: no agent skills folder here; to use the skill, link $root/skills/cronward" \
    "into your agent's skills folder"
fi

case ":$PATH:" in
  *":$dir:"*) ;;
  *) echo "note: $dir is not on your PATH" ;;
esac
