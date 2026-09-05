"""Consistent online SQLite backup. Save .env separately in a safe place."""
import argparse
from datetime import datetime, timezone
from pathlib import Path
import sqlite3

parser = argparse.ArgumentParser()
parser.add_argument('--database', default='data/spend/spend.sqlite')
parser.add_argument('--output-dir', default='backups')
args = parser.parse_args()
source = Path(args.database).resolve()
if not source.is_file():
    raise SystemExit('Database does not exist')
directory = Path(args.output_dir)
directory.mkdir(parents=True, exist_ok=True)
target = directory / ('spend-' + datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S-%f') + '.sqlite')
with sqlite3.connect(source.as_uri() + '?mode=ro', uri=True) as src, sqlite3.connect(target) as dst:
    src.backup(dst)
target.chmod(0o600)
print(target)
