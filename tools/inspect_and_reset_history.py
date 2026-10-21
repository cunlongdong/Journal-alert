"""Read-only by default: inspect/reset the SQLite *seen* index with a local backup.

IMPORTANT: Does not delete reports, bilingual caches or delivery queues, and does
not send notifications. Use only after disabling the scheduled workflow and
agreeing on the intended scope of history removal.
"""
from __future__ import annotations
import argparse
import json
import shutil
import sqlite3
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def inspect_or_reset(db: Path, *, before: str | None = None, all_rows: bool = False,
                     apply: bool = False, confirm: str = '') -> dict:
    db = Path(db)
    if not db.is_file():
        raise FileNotFoundError(f'SQLite file not found: {db}')
    if before and all_rows:
        raise ValueError('--before and --all are mutually exclusive')
    if not before and not all_rows:
        raise ValueError('Choose either --before YYYY-MM-DD or --all')
    if before:
        # Date is on first_seen (when journal-alert discovered the work), not publication date.
        datetime.strptime(before, '%Y-%m-%d')
    with sqlite3.connect(db) as con:
        total = con.execute('SELECT COUNT(*) FROM articles').fetchone()[0]
        if before:
            eligible = con.execute('SELECT COUNT(*) FROM articles WHERE first_seen < ?', (before,)).fetchone()[0]
        else:
            eligible = total
        preview = {'database': str(db), 'records_total': total, 'records_selected': eligible,
                   'scope': 'all' if all_rows else f'first_seen earlier than {before}',
                   'would_delete': eligible, 'applied': False}
        if not apply:
            return preview
        if confirm != 'DELETE_HISTORY_AFTER_BACKUP':
            raise ValueError('Refusing deletion: supply --confirm DELETE_HISTORY_AFTER_BACKUP')
        # sqlite3 backup API writes a consistent snapshot, unlike a plain copy of an open DB.
        backup = db.with_name(db.name + '.bak-' + datetime.now().strftime('%Y%m%dT%H%M%S'))
        with sqlite3.connect(backup) as out:
            con.backup(out)
        con.execute('BEGIN IMMEDIATE')
        try:
            if before:
                con.execute('DELETE FROM articles WHERE first_seen < ?', (before,))
            else:
                con.execute('DELETE FROM articles')
                con.execute('DELETE FROM runs')
            con.commit()
        except BaseException:
            con.rollback()
            raise
        preview['applied'] = True
        preview['backup'] = str(backup)
        preview['remaining'] = con.execute('SELECT COUNT(*) FROM articles').fetchone()[0]
        return preview


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description='Preview or safely remove seen.sqlite history rows')
    scope = p.add_mutually_exclusive_group(required=True)
    scope.add_argument('--before', help='Only first_seen earlier than YYYY-MM-DD')
    scope.add_argument('--all', action='store_true', help='Select every article')
    p.add_argument('--db', type=Path, default=ROOT / 'state' / 'seen.sqlite')
    p.add_argument('--apply', action='store_true', help='Perform deletion after backing up')
    p.add_argument('--confirm', default='', help='Required confirmation for --apply')
    ns = p.parse_args(argv)
    print(json.dumps(inspect_or_reset(ns.db, before=ns.before, all_rows=ns.all,
                                      apply=ns.apply, confirm=ns.confirm), ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
