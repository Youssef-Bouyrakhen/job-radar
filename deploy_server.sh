#!/bin/bash
# The server half of deploy.bat (it sends this file over ssh; you don't run it yourself).
#   backup           : copy the database to data/backup-<date>.db and check the copy
#   finish <version> : install packages, restart Job Radar, check that it runs <version>
# Never touches data/jobradar.db, .env or config.yaml.
set -euo pipefail
APP=/home/jobradar/job-radar
PY=$APP/.venv/bin/python
cd "$APP"

case "${1:-}" in
  backup)
    FILE="data/backup-$(date +%Y%m%d-%H%M%S).db"
    # SQLite's own backup: safe while Job Radar keeps running
    sudo -u jobradar -H "$PY" -c '
import sqlite3, sys
src, dst = sqlite3.connect("data/jobradar.db"), sqlite3.connect(sys.argv[1])
src.backup(dst); dst.close()
check = sqlite3.connect(sys.argv[1]).execute("PRAGMA integrity_check").fetchone()[0]
print("   Backup:", sys.argv[1], "(check:", check + ")")
sys.exit(check != "ok")' "$FILE"
    ;;
  finish)
    WANT="${2:?version missing}"
    echo "   Installing packages..."
    sudo -u jobradar -H "$PY" -m pip install -q --disable-pip-version-check -r requirements.txt
    echo "   Restarting Job Radar..."
    systemctl restart jobradar
    # Job Radar needs about a minute to start (it checks the jobs first): wait up to 2 minutes
    VERSION=""
    START=$SECONDS
    echo "   Waiting for Job Radar to answer (up to 2 minutes)..."
    while [ $((SECONDS - START)) -lt 120 ]; do
      VERSION=$("$PY" -c 'import json, urllib.request
print(json.load(urllib.request.urlopen("http://127.0.0.1:8000/api/info", timeout=5))["version"])' 2>/dev/null) && break
      sleep 3
    done
    [ -n "$VERSION" ] && echo "   It answered after $((SECONDS - START)) s."
    if [ "$VERSION" = "$WANT" ]; then
      echo "   The server now runs version $VERSION."
    else
      echo "   PROBLEM: the server answers version '${VERSION:-nothing}', expected $WANT. Last log lines:"
      journalctl -u jobradar -n 15 --no-pager
      exit 1
    fi
    ;;
  *)
    echo "usage: deploy_server.sh backup | finish <version>"; exit 2 ;;
esac
