import argparse
import json
import pathlib
import re
import sys
import traceback
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple
from urllib.parse import urlsplit, urlunsplit

from sqlalchemy import create_engine, inspect
from sqlalchemy.schema import CheckConstraint, ForeignKeyConstraint, Index, UniqueConstraint

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.database import Base  # noqa: E402
from app.database import SQLALCHEMY_DATABASE_URL  # noqa: E402
from app import models  # noqa: F401,E402
from app.modules.ai_factory import models as ai_factory_models  # noqa: F401,E402
from app.modules.bible_video_factory import models as bible_video_factory_models  # noqa: F401,E402
from app.modules.humor_factory import models as humor_factory_models  # noqa: F401,E402


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


EXPECTED_EXTRA_DB_TABLES = {
    "alembic_version",
    "ai_capability_policies",
    "ai_operation_cache",
    "ai_operation_runs",
    "ai_provider_circuit_breakers",
    "codexia_asset_generation_cache",
    "codexia_financial_audit_events",
    "codexia_financial_ledger_entries",
}


def _is_equivalent_type(orm_type: str, db_type: str) -> bool:
    a = str(orm_type or "").strip().lower()
    b = str(db_type or "").strip().lower()
    if a == b:
        return True
    float_aliases = {
        "float": "double precision",
        "double precision": "double precision",
        "real": "real",
    }
    if a in float_aliases and b in float_aliases:
        return float_aliases[a] == float_aliases[b] or {a, b} <= {"float", "double precision"}
    return False


def _is_equivalent_default(orm_default: Optional[str], db_default: Optional[str], orm_col: Any) -> bool:
    a = _normalize_sql(orm_default)
    b = _normalize_sql(db_default)
    if a == b:
        return True
    if getattr(orm_col, "primary_key", False) and a is None and isinstance(b, str) and b.startswith("nextval"):
        return True
    return False


def _mask_database_url(database_url: str) -> str:
    try:
        parts = urlsplit(database_url)
        if not parts.username and not parts.password:
            return database_url
        netloc = parts.hostname or ""
        if parts.port:
            netloc = f"{netloc}:{parts.port}"
        if parts.username:
            netloc = f"{parts.username}:***@{netloc}"
        return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))
    except Exception:
        return "<invalid_database_url>"


def _normalize_sql(text: Optional[str]) -> Optional[str]:
    if text is None:
        return None
    value = str(text).strip()
    value = re.sub(r"\s+", " ", value)
    value = value.strip("() ")
    return value.lower()


def _type_sql(type_: Any, dialect: Any) -> str:
    try:
        return str(type_.compile(dialect=dialect)).strip().lower()
    except Exception:
        try:
            return str(type_).strip().lower()
        except Exception:
            return "<unknown>"


def _column_default_from_orm(col: Any) -> Optional[str]:
    if getattr(col, "server_default", None) is None:
        return None
    arg = getattr(col.server_default, "arg", None)
    if arg is None:
        return None
    return str(arg)


@dataclass(frozen=True)
class ForeignKeySig:
    constrained_columns: Tuple[str, ...]
    referred_table: str
    referred_columns: Tuple[str, ...]
    ondelete: Optional[str]
    onupdate: Optional[str]


@dataclass(frozen=True)
class IndexSig:
    columns: Tuple[str, ...]
    unique: bool
    where: Optional[str]


@dataclass(frozen=True)
class UniqueSig:
    columns: Tuple[str, ...]


@dataclass(frozen=True)
class CheckSig:
    sqltext: str


def _orm_foreign_keys(table) -> Dict[ForeignKeySig, List[str]]:
    result: Dict[ForeignKeySig, List[str]] = {}
    for constraint in table.foreign_key_constraints:
        if not isinstance(constraint, ForeignKeyConstraint):
            continue
        constrained_cols = tuple(col.name for col in constraint.columns)
        referred_table = constraint.elements[0].column.table.name if constraint.elements else ""
        referred_cols = tuple(elem.column.name for elem in constraint.elements)
        options = constraint.dialect_options.get("postgresql", {})
        ondelete = getattr(constraint, "ondelete", None) or options.get("ondelete")
        onupdate = getattr(constraint, "onupdate", None) or options.get("onupdate")
        sig = ForeignKeySig(
            constrained_columns=constrained_cols,
            referred_table=referred_table,
            referred_columns=referred_cols,
            ondelete=(str(ondelete).lower() if ondelete else None),
            onupdate=(str(onupdate).lower() if onupdate else None),
        )
        result.setdefault(sig, []).append(constraint.name or "")
    return result


def _db_foreign_keys(inspector, table_name: str, schema: Optional[str]) -> Dict[ForeignKeySig, List[str]]:
    result: Dict[ForeignKeySig, List[str]] = {}
    for fk in inspector.get_foreign_keys(table_name, schema=schema) or []:
        options = fk.get("options") or {}
        sig = ForeignKeySig(
            constrained_columns=tuple(fk.get("constrained_columns") or []),
            referred_table=str(fk.get("referred_table") or ""),
            referred_columns=tuple(fk.get("referred_columns") or []),
            ondelete=(str(options.get("ondelete")).lower() if options.get("ondelete") else None),
            onupdate=(str(options.get("onupdate")).lower() if options.get("onupdate") else None),
        )
        result.setdefault(sig, []).append(str(fk.get("name") or ""))
    return result


def _orm_indexes(table) -> Dict[IndexSig, List[str]]:
    result: Dict[IndexSig, List[str]] = {}
    for idx in table.indexes:
        if not isinstance(idx, Index):
            continue
        where = None
        dialect_opts = idx.dialect_options.get("postgresql") or {}
        if dialect_opts.get("where") is not None:
            where = str(dialect_opts.get("where"))
        sig = IndexSig(
            columns=tuple(col.name for col in idx.columns),
            unique=bool(getattr(idx, "unique", False)),
            where=_normalize_sql(where),
        )
        result.setdefault(sig, []).append(idx.name or "")
    return result


def _db_indexes(inspector, table_name: str, schema: Optional[str]) -> Dict[IndexSig, List[str]]:
    result: Dict[IndexSig, List[str]] = {}
    unique_cols = {
        tuple((uc.get("column_names") or []))
        for uc in (inspector.get_unique_constraints(table_name, schema=schema) or [])
    }
    for idx in inspector.get_indexes(table_name, schema=schema) or []:
        dialect_opts = idx.get("dialect_options") or {}
        where = None
        if isinstance(dialect_opts, dict):
            where = dialect_opts.get("postgresql_where") or dialect_opts.get("where")
        columns = tuple(idx.get("column_names") or [])
        is_unique = bool(idx.get("unique", False))
        if is_unique and not _normalize_sql(where) and columns in unique_cols:
            continue
        sig = IndexSig(
            columns=columns,
            unique=is_unique,
            where=_normalize_sql(where),
        )
        result.setdefault(sig, []).append(str(idx.get("name") or ""))
    return result


def _orm_uniques(table) -> Dict[UniqueSig, List[str]]:
    result: Dict[UniqueSig, List[str]] = {}
    for constraint in table.constraints:
        if not isinstance(constraint, UniqueConstraint):
            continue
        sig = UniqueSig(columns=tuple(col.name for col in constraint.columns))
        result.setdefault(sig, []).append(constraint.name or "")
    return result


def _db_uniques(inspector, table_name: str, schema: Optional[str]) -> Dict[UniqueSig, List[str]]:
    result: Dict[UniqueSig, List[str]] = {}
    for uc in inspector.get_unique_constraints(table_name, schema=schema) or []:
        sig = UniqueSig(columns=tuple(uc.get("column_names") or []))
        result.setdefault(sig, []).append(str(uc.get("name") or ""))
    return result


def _orm_checks(table) -> Dict[CheckSig, List[str]]:
    result: Dict[CheckSig, List[str]] = {}
    for constraint in table.constraints:
        if not isinstance(constraint, CheckConstraint):
            continue
        sqltext = getattr(constraint.sqltext, "text", None) or str(constraint.sqltext)
        normalized = _normalize_sql(sqltext) or ""
        sig = CheckSig(sqltext=normalized)
        result.setdefault(sig, []).append(constraint.name or "")
    return result


def _db_checks(inspector, table_name: str, schema: Optional[str]) -> Dict[CheckSig, List[str]]:
    result: Dict[CheckSig, List[str]] = {}
    for ck in inspector.get_check_constraints(table_name, schema=schema) or []:
        normalized = _normalize_sql(ck.get("sqltext")) or ""
        sig = CheckSig(sqltext=normalized)
        result.setdefault(sig, []).append(str(ck.get("name") or ""))
    return result


def _diff_signature_maps(
    orm_map: Dict[Any, List[str]],
    db_map: Dict[Any, List[str]],
) -> Dict[str, Any]:
    orm_keys = set(orm_map.keys())
    db_keys = set(db_map.keys())
    missing_in_db = sorted(orm_keys - db_keys, key=str)
    extra_in_db = sorted(db_keys - orm_keys, key=str)
    name_mismatches = []
    for key in sorted(orm_keys & db_keys, key=str):
        orm_names = sorted([n for n in orm_map.get(key, []) if n])
        db_names = sorted([n for n in db_map.get(key, []) if n])
        if orm_names and db_names and orm_names != db_names:
            name_mismatches.append({"signature": str(key), "orm_names": orm_names, "db_names": db_names})
    return {
        "missing_in_db": [str(x) for x in missing_in_db],
        "extra_in_db": [str(x) for x in extra_in_db],
        "name_mismatches": name_mismatches,
    }


def compare_schema(database_url: str, schema: Optional[str]) -> Dict[str, Any]:
    engine = create_engine(database_url)
    inspector = inspect(engine)

    orm_tables = {table.name: table for table in Base.metadata.sorted_tables}
    db_tables = set(inspector.get_table_names(schema=schema) or [])

    missing_tables = sorted(set(orm_tables.keys()) - db_tables)
    extra_tables_all = sorted(db_tables - set(orm_tables.keys()))
    extra_tables_expected = sorted([t for t in extra_tables_all if t in EXPECTED_EXTRA_DB_TABLES])
    extra_tables_unexpected = sorted([t for t in extra_tables_all if t not in EXPECTED_EXTRA_DB_TABLES])

    table_reports: Dict[str, Any] = {}
    for table_name, table in sorted(orm_tables.items(), key=lambda x: x[0]):
        if table_name not in db_tables:
            continue

        db_cols = {c["name"]: c for c in inspector.get_columns(table_name, schema=schema) or []}
        orm_cols = {c.name: c for c in table.columns}

        missing_cols = sorted(set(orm_cols.keys()) - set(db_cols.keys()))
        extra_cols = sorted(set(db_cols.keys()) - set(orm_cols.keys()))

        col_diffs = []
        for col_name in sorted(set(orm_cols.keys()) & set(db_cols.keys())):
            orm_col = orm_cols[col_name]
            db_col = db_cols[col_name]

            orm_type = _type_sql(orm_col.type, engine.dialect)
            db_type = _type_sql(db_col.get("type"), engine.dialect)

            orm_nullable = bool(getattr(orm_col, "nullable", True))
            db_nullable = bool(db_col.get("nullable", True))

            orm_default = _normalize_sql(_column_default_from_orm(orm_col))
            db_default = _normalize_sql(db_col.get("default"))

            issues = {}
            if not _is_equivalent_type(orm_type, db_type):
                issues["type"] = {"orm": orm_type, "db": db_type}
            if orm_nullable != db_nullable:
                issues["nullable"] = {"orm": orm_nullable, "db": db_nullable}
            if not _is_equivalent_default(orm_default, db_default, orm_col):
                issues["default"] = {"orm": orm_default, "db": db_default}

            if issues:
                col_diffs.append({"column": col_name, "issues": issues})

        fk_report = _diff_signature_maps(
            _orm_foreign_keys(table),
            _db_foreign_keys(inspector, table_name, schema),
        )
        idx_report = _diff_signature_maps(
            _orm_indexes(table),
            _db_indexes(inspector, table_name, schema),
        )
        unique_report = _diff_signature_maps(
            _orm_uniques(table),
            _db_uniques(inspector, table_name, schema),
        )
        check_report = _diff_signature_maps(
            _orm_checks(table),
            _db_checks(inspector, table_name, schema),
        )

        table_reports[table_name] = {
            "columns": {
                "missing_in_db": missing_cols,
                "extra_in_db": extra_cols,
                "mismatches": col_diffs,
            },
            "foreign_keys": fk_report,
            "indexes": idx_report,
            "unique_constraints": unique_report,
            "check_constraints": check_report,
        }

    total_column_mismatches = sum(len(t["columns"]["mismatches"]) for t in table_reports.values())
    total_missing_columns = sum(len(t["columns"]["missing_in_db"]) for t in table_reports.values())
    total_extra_columns = sum(len(t["columns"]["extra_in_db"]) for t in table_reports.values())

    def _count_sig_issues(section: str) -> int:
        count = 0
        for t in table_reports.values():
            count += len(t[section]["missing_in_db"])
            count += len(t[section]["extra_in_db"])
            count += len(t[section]["name_mismatches"])
        return count

    summary = {
        "tables_missing_in_db": len(missing_tables),
        "tables_extra_in_db": len(extra_tables_unexpected),
        "tables_extra_expected": len(extra_tables_expected),
        "columns_missing_in_db": total_missing_columns,
        "columns_extra_in_db": total_extra_columns,
        "column_mismatches": total_column_mismatches,
        "foreign_key_issues": _count_sig_issues("foreign_keys"),
        "index_issues": _count_sig_issues("indexes"),
        "unique_constraint_issues": _count_sig_issues("unique_constraints"),
        "check_constraint_issues": _count_sig_issues("check_constraints"),
    }

    return {
        "generated_at": _now_iso(),
        "database_url": _mask_database_url(database_url),
        "schema": schema or "default",
        "summary": summary,
        "tables": {
            "missing_in_db": missing_tables,
            "extra_in_db": extra_tables_unexpected,
            "extra_expected": extra_tables_expected,
        },
        "table_reports": table_reports,
        "notes": [
            "Comparação de defaults considera apenas server_default (ORM) vs default (PostgreSQL). Defaults Python-side não entram.",
            "Comparação de tipos usa SQL compilado pelo dialect do SQLAlchemy; casts (::type) podem gerar divergências de string mesmo sendo equivalentes no PostgreSQL.",
            "Para índices parciais (WHERE), a normalização é textual e pode exigir revisão manual.",
        ],
    }


def _render_markdown(report: Dict[str, Any]) -> str:
    lines: List[str] = []
    summary = report.get("summary") or {}

    lines.append("# Relatório: ORM vs PostgreSQL")
    lines.append("")
    lines.append(f"- Gerado em: {report.get('generated_at')}")
    lines.append(f"- Database: {report.get('database_url')}")
    lines.append(f"- Schema: {report.get('schema')}")
    lines.append("")
    lines.append("## Resumo")
    lines.append("")
    for k in [
        "tables_missing_in_db",
        "tables_extra_in_db",
        "tables_extra_expected",
        "columns_missing_in_db",
        "columns_extra_in_db",
        "column_mismatches",
        "foreign_key_issues",
        "index_issues",
        "unique_constraint_issues",
        "check_constraint_issues",
    ]:
        lines.append(f"- {k}: {summary.get(k, 0)}")

    tables = report.get("tables") or {}
    if tables.get("missing_in_db") or tables.get("extra_in_db") or tables.get("extra_expected"):
        lines.append("")
        lines.append("## Tabelas")
        lines.append("")
        if tables.get("missing_in_db"):
            lines.append("- Ausentes no banco:")
            for t in tables["missing_in_db"]:
                lines.append(f"  - {t}")
        if tables.get("extra_in_db"):
            lines.append("- Extras no banco:")
            for t in tables["extra_in_db"]:
                lines.append(f"  - {t}")
        if tables.get("extra_expected"):
            lines.append("- Extras esperados no banco (não mapeados no ORM):")
            for t in tables["extra_expected"]:
                lines.append(f"  - {t}")

    table_reports: Dict[str, Any] = report.get("table_reports") or {}
    interesting_tables = []
    for table_name, payload in table_reports.items():
        cols = payload.get("columns") or {}
        has_cols = bool(cols.get("missing_in_db") or cols.get("extra_in_db") or cols.get("mismatches"))
        has_fk = any(payload.get("foreign_keys", {}).get(k) for k in ("missing_in_db", "extra_in_db", "name_mismatches"))
        has_idx = any(payload.get("indexes", {}).get(k) for k in ("missing_in_db", "extra_in_db", "name_mismatches"))
        has_unique = any(
            payload.get("unique_constraints", {}).get(k) for k in ("missing_in_db", "extra_in_db", "name_mismatches")
        )
        has_check = any(
            payload.get("check_constraints", {}).get(k) for k in ("missing_in_db", "extra_in_db", "name_mismatches")
        )
        if has_cols or has_fk or has_idx or has_unique or has_check:
            interesting_tables.append(table_name)

    if interesting_tables:
        lines.append("")
        lines.append("## Divergências por tabela")
        lines.append("")

    for table_name in interesting_tables:
        payload = table_reports[table_name]
        lines.append(f"### {table_name}")
        lines.append("")

        cols = payload.get("columns") or {}
        if cols.get("missing_in_db") or cols.get("extra_in_db") or cols.get("mismatches"):
            lines.append("- Colunas")
            if cols.get("missing_in_db"):
                lines.append("  - Ausentes no banco: " + ", ".join(cols["missing_in_db"]))
            if cols.get("extra_in_db"):
                lines.append("  - Extras no banco: " + ", ".join(cols["extra_in_db"]))
            for mismatch in cols.get("mismatches") or []:
                parts = []
                for issue, values in (mismatch.get("issues") or {}).items():
                    parts.append(f"{issue}: orm={values.get('orm')} db={values.get('db')}")
                lines.append(f"  - {mismatch.get('column')}: " + "; ".join(parts))

        def _section(title: str, key: str) -> None:
            data = payload.get(key) or {}
            if not (data.get("missing_in_db") or data.get("extra_in_db") or data.get("name_mismatches")):
                return
            lines.append(f"- {title}")
            if data.get("missing_in_db"):
                lines.append("  - Ausentes no banco:")
                for item in data["missing_in_db"]:
                    lines.append(f"    - {item}")
            if data.get("extra_in_db"):
                lines.append("  - Extras no banco:")
                for item in data["extra_in_db"]:
                    lines.append(f"    - {item}")
            if data.get("name_mismatches"):
                lines.append("  - Mesma assinatura, nomes divergentes:")
                for item in data["name_mismatches"]:
                    lines.append(f"    - {item.get('signature')}")
                    lines.append(f"      - orm: {', '.join(item.get('orm_names') or [])}")
                    lines.append(f"      - db: {', '.join(item.get('db_names') or [])}")

        _section("Foreign Keys", "foreign_keys")
        _section("Índices", "indexes")
        _section("Unique Constraints", "unique_constraints")
        _section("Check Constraints", "check_constraints")
        lines.append("")

    notes = report.get("notes") or []
    if notes:
        lines.append("## Notas")
        lines.append("")
        for note in notes:
            lines.append(f"- {note}")
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def _parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Comparador ORM (SQLAlchemy) vs PostgreSQL (reflexão).")
    parser.add_argument("--database-url", default="", help="Opcional. Caso vazio, usa DATABASE_URL configurada.")
    parser.add_argument("--schema", default="", help="Schema do PostgreSQL (ex: public). Vazio usa default.")
    parser.add_argument(
        "--format",
        default="json",
        choices=["json", "markdown"],
        help="Formato do relatório.",
    )
    parser.add_argument("--output", default="", help="Arquivo de saída. Vazio imprime no stdout.")
    return parser.parse_args(list(argv))


def main(argv: Sequence[str]) -> int:
    args = _parse_args(argv)
    database_url = (args.database_url or "").strip() or SQLALCHEMY_DATABASE_URL
    schema = (args.schema or "").strip() or None
    try:
        report = compare_schema(database_url, schema=schema)
        if args.format == "markdown":
            content = _render_markdown(report)
        else:
            content = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"

        if args.output:
            out_path = pathlib.Path(args.output)
            out_path.write_text(content, encoding="utf-8")
        else:
            sys.stdout.write(content)
        return 0
    except Exception as exc:
        sys.stderr.write(f"[compare_schema] falha: {exc}\n")
        sys.stderr.write(traceback.format_exc())
        return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
