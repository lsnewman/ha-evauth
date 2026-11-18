#!/usr/bin/env bash
# Fails if any file that git would commit contains a denylisted pattern.
# The denylist lives in private/denylist.txt (gitignored): one extended regex per line.
# Usage: scripts/check_sensitive.sh            (checks staged + tracked + untracked-not-ignored)
set -euo pipefail
root="$(git rev-parse --show-toplevel)"
cd "$root"
deny="private/denylist.txt"
if [ ! -f "$deny" ]; then
  echo "check_sensitive: $deny missing; refusing to pass" >&2
  exit 2
fi
pattern="$(grep -v '^\s*$' "$deny" | grep -v '^#' | paste -sd'|' -)"
status=0
# files git would include: tracked + untracked-but-not-ignored
while IFS= read -r f; do
  [ -f "$f" ] || continue
  if grep -I -n -i -E -- "$pattern" "$f" >/tmp/.evauth_hits 2>/dev/null; then
    echo "SENSITIVE in $f:" >&2
    sed 's/^/    /' /tmp/.evauth_hits >&2
    status=1
  fi
done < <(git ls-files --cached --others --exclude-standard)
rm -f /tmp/.evauth_hits
[ $status -eq 0 ] && echo "check_sensitive: clean"
exit $status
