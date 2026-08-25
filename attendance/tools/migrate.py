#!/usr/bin/env python3
"""공용 스키마 + 모든 매장 스키마 마이그레이션.

    python3 tools/migrate.py            # 전부 최신으로
    python3 tools/migrate.py --status   # 버전만 확인

매장이 늘면 마이그레이션도 매장 수만큼 돌아갑니다. 한 매장만 실패해 버전이
어긋나는 상황이 D4 의 실질 비용이라, --status 로 항상 확인할 수 있게 했습니다.
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from sqlalchemy import select, text  # noqa: E402

from app.config import CONTROL_SCHEMA, schema_for  # noqa: E402
from app.db import control_session, engine  # noqa: E402
from app.models.control import Store  # noqa: E402


def _alembic(ini: str, *extra: str) -> int:
    cmd = [sys.executable, "-m", "alembic", "-c", ini, *extra]
    return subprocess.call(cmd, cwd=ROOT)


def _head(tree: str) -> str:
    versions = ROOT / "migrations" / tree / "versions"
    revs = {}
    downs = set()
    for f in versions.glob("*.py"):
        body = f.read_text(encoding="utf-8")
        rid = _find(body, "revision = ")
        down = _find(body, "down_revision = ")
        if rid:
            revs[rid] = f.name
        if down:
            downs.add(down)
    heads = [r for r in revs if r not in downs]
    return heads[0] if len(heads) == 1 else ""


def _find(body: str, prefix: str) -> str:
    for line in body.splitlines():
        if line.startswith(prefix):
            value = line[len(prefix):].strip()
            return "" if value == "None" else value.strip("'\"")
    return ""


def _current(schema: str) -> str:
    with engine.connect() as c:
        exists = c.execute(
            text(
                "SELECT 1 FROM information_schema.tables "
                "WHERE table_schema=:s AND table_name='alembic_version'"
            ),
            {"s": schema},
        ).scalar()
        if not exists:
            return "(없음)"
        return c.execute(text(f'SELECT version_num FROM "{schema}".alembic_version')).scalar() or "(비어있음)"


def _stores() -> list[Store]:
    with control_session() as s:
        rows = list(s.execute(select(Store).order_by(Store.slug)).scalars())
        for r in rows:
            s.expunge(r)
        return rows


def status() -> int:
    ctrl_head, store_head = _head("control"), _head("store")
    print(f"{'대상':<22}{'현재':<16}{'최신':<16}상태")
    print("-" * 66)
    rows = [(CONTROL_SCHEMA, _current(CONTROL_SCHEMA), ctrl_head)]
    for st in _stores():
        rows.append((schema_for(st.slug), _current(schema_for(st.slug)), store_head))
    drift = 0
    for name, cur, head in rows:
        ok = cur == head and head
        drift += 0 if ok else 1
        print(f"{name:<22}{cur[:14]:<16}{(head or '?')[:14]:<16}{'최신' if ok else '갱신 필요'}")
    if drift:
        print(f"\n{drift}개가 최신이 아닙니다. `python3 tools/migrate.py` 를 실행하세요.")
    return 1 if drift else 0


def upgrade_all() -> int:
    print(f"[control] {CONTROL_SCHEMA}")
    rc = _alembic("alembic_control.ini", "upgrade", "head")
    if rc:
        print("공용 스키마 마이그레이션 실패. 매장은 건너뜁니다.", file=sys.stderr)
        return rc
    failed = []
    for st in _stores():
        schema = schema_for(st.slug)
        print(f"[store]   {schema}  ({st.name})")
        if _alembic("alembic_store.ini", "-x", f"schema={schema}", "upgrade", "head"):
            failed.append(schema)
    if failed:
        print(f"\n실패한 매장: {', '.join(failed)}", file=sys.stderr)
        return 1
    print("\n전부 최신입니다.")
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--status", action="store_true", help="버전만 확인하고 끝냅니다")
    args = ap.parse_args()
    raise SystemExit(status() if args.status else upgrade_all())
