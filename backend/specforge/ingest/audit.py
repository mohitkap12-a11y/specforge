"""Completeness audit: the evidence behind a "no gaps" claim for parsing.

Every check yields zero or more issues {severity, kind, node_key, page, message}. A parse run is
"coverage-ready" when it has no open error-level issues.
"""
from __future__ import annotations

from collections import Counter

from .ir import ParsedSpec
from .normalize import classify_table, parse_bit_range, parse_byte_range, parse_value_range

TEXT_ACCOUNTING_MIN = 0.995


def audit_spec(spec: ParsedSpec) -> tuple[list[dict], dict]:
    issues: list[dict] = list(spec.notes)
    checks: dict[str, dict] = {}

    # 1. section coverage: every bookmark/heading became a section (heading_not_found already raised by the parser)
    missing = [i for i in issues if i["kind"] == "heading_not_found"]
    checks["section_coverage"] = {"pass": not missing, "detail": f"{max(0, len(spec.sections) - 1)} sections, {len(missing)} not found in text"}

    # 2. text accounting
    raw, acc = spec.raw_char_count, spec.accounted_char_count
    ratio = (acc / raw) if raw else 1.0
    ok = ratio >= TEXT_ACCOUNTING_MIN and (raw == 0 or acc <= raw * 1.02)
    if not ok:
        issues.append({"severity": "error", "kind": "text_accounting", "node_key": None, "page": None,
                       "message": f"Only {acc} of {raw} non-whitespace characters are accounted for ({ratio:.2%})"})
    checks["text_accounting"] = {"pass": ok, "detail": f"{acc}/{raw} characters accounted ({ratio:.2%})"}

    # 3. figure accounting + references
    labels = {t.label.split("#")[0] for t in spec.tables}
    unresolved = 0
    for r in spec.references:
        if r.kind == "figure" and r.target_family is None and r.target_label not in labels:
            unresolved += 1
            issues.append({"severity": "error", "kind": "figure_reference_missing", "node_key": r.src_key, "page": r.page,
                           "message": f"'{r.raw}' refers to Figure/Table {r.target_label}, which was not found in this document"})
    checks["figure_references"] = {"pass": unresolved == 0, "detail": f"{len(labels)} tables/figures; {unresolved} dangling intra-document references"}

    # 4. table schema checks
    bad_tables = 0
    for t in spec.tables:
        kind = classify_table(t.header, t.caption)
        key = f"fig:{t.label}"
        if kind == "cdw":
            rngs = [parse_bit_range(r[0]) for r in t.rows if r]
            if any(x is None for x in rngs):
                bad_tables += 1
                issues.append({"severity": "error", "kind": "bit_range_unparsed", "node_key": key, "page": t.page_from,
                               "message": f"Figure {t.label}: a Bits cell could not be parsed as a bit range"})
                continue
            covered = Counter()
            for hi, lo in rngs:  # type: ignore[misc]
                for b in range(lo, hi + 1):
                    covered[b] += 1
            over = sorted(b for b, c in covered.items() if c > 1)
            if over:
                bad_tables += 1
                issues.append({"severity": "error", "kind": "bit_range_overlap", "node_key": key, "page": t.page_from,
                               "message": f"Figure {t.label}: bits {over} are described more than once"})
            gaps = [b for b in range(32) if b not in covered]
            if gaps and not over:
                issues.append({"severity": "warning", "kind": "bit_range_gap", "node_key": key, "page": t.page_from,
                               "message": f"Figure {t.label}: bits {gaps} are not described (expected reserved rows)"})
        elif kind == "bytes":
            rngs = [parse_byte_range(r[0]) for r in t.rows if r]
            if any(x is None for x in rngs):
                bad_tables += 1
                issues.append({"severity": "error", "kind": "byte_range_unparsed", "node_key": key, "page": t.page_from,
                               "message": f"Figure {t.label}: a Bytes cell could not be parsed"})
                continue
            used = Counter()
            for hi, lo in rngs:  # type: ignore[misc]
                for b in range(lo, hi + 1):
                    used[b] += 1
            if any(c > 1 for c in used.values()):
                bad_tables += 1
                issues.append({"severity": "error", "kind": "byte_range_overlap", "node_key": key, "page": t.page_from,
                               "message": f"Figure {t.label}: overlapping byte ranges"})
        elif kind in ("values", "opcode"):
            seen: Counter = Counter()
            for r in t.rows:
                if r and r[0].strip():
                    rg = parse_value_range(r[0])
                    seen[rg if rg else r[0].strip().lower()] += 1
            dup = [k for k, c in seen.items() if c > 1]
            if dup:
                bad_tables += 1
                issues.append({"severity": "error", "kind": "duplicate_values", "node_key": key, "page": t.page_from,
                               "message": f"Figure {t.label}: duplicate value/opcode rows {dup[:5]}"})
            if kind == "values" and seen and sum(1 for k in seen if isinstance(k, str)) > 0.5 * len(seen):
                issues.append({"severity": "warning", "kind": "values_unparsed", "node_key": key, "page": t.page_from,
                               "message": f"Figure {t.label}: most value cells do not parse as numeric values"})
    checks["table_schema"] = {"pass": bad_tables == 0, "detail": f"{len(spec.tables)} tables checked, {bad_tables} with schema errors"}

    errors = sum(1 for i in issues if i["severity"] == "error")
    warnings = sum(1 for i in issues if i["severity"] == "warning")
    passed = sum(1 for c in checks.values() if c["pass"])
    summary = {"checks": checks, "errors": errors, "warnings": warnings,
               "score": round(passed / max(1, len(checks)), 3), "coverage_ready": errors == 0}
    return issues, summary
