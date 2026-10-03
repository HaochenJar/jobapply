#!/bin/bash
# Install the jobapply engine into ~/jobapply. Safe to re-run: never overwrites an existing file,
# so your database, applications/, docs/master.md and personalized docs.py/score.py survive.
set -euo pipefail
SRC="$(cd "$(dirname "$0")/engine" && pwd)"
DEST="$HOME/jobapply"
CFG="$HOME/.config/jobapply"

mkdir -p "$DEST" "$CFG"
rsync -a --ignore-existing --exclude __pycache__ "$SRC/" "$DEST/"
chmod +x "$DEST/run.sh"
if [ ! -f "$CFG/profile.json" ]; then
  cp "$SRC/profile.example.json" "$CFG/profile.json"
  chmod 600 "$CFG/profile.json"
  echo "created $CFG/profile.json -- fill it in by hand (see $DEST/INTAKE.md)"
fi
(cd "$DEST" && python3 db.py init)

echo "engine: $DEST"
python3 -c 'import sys; sys.exit(sys.version_info < (3, 9))' || echo "MISSING: Python 3.9+"
command -v pandoc >/dev/null || echo "MISSING: pandoc (brew install pandoc) -- needed to render resumes"
[ -x "${CHROME:-/Applications/Google Chrome.app/Contents/MacOS/Google Chrome}" ] \
  || echo "MISSING: Google Chrome (or set CHROME=/path/to/chrome) -- needed to render PDFs"
[ -f "$DEST/docs/master.md" ] || echo "TODO: create $DEST/docs/master.md (format: docs/master.example.md)"
