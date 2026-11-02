#!/usr/bin/env bash
# Links bin/cronward into ~/.local/bin. Safe to run again.
set -euo pipefail

src="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)/bin/cronward"
dir="$HOME/.local/bin"
dest="$dir/cronward"

if [[ -L $dest ]]; then
  old="$(readlink "$dest")"
  if [[ $old == "$src" ]]; then
    echo "$dest already links to $src"
    exit 0
  fi
  echo "replacing link to $old"
elif [[ -e $dest ]]; then
  echo "install.sh: $dest exists and is not a link, leaving it alone" >&2
  exit 1
fi

mkdir -p "$dir"
ln -sfn "$src" "$dest"
echo "linked $dest -> $src"

case ":$PATH:" in
  *":$dir:"*) ;;
  *) echo "note: $dir is not on your PATH" ;;
esac
