"""Read-only MOD and ISO KPI snapshots from the five-table architecture.

Comparison approval permits presentation of *estimates*, never measured savings or
full source verification. Test fixtures are explicitly labelled, not filtered away.
No legacy tables/views are consulted, and no source document bytes are loaded.
"""

from decimal import Decimal, InvalidOperation
import re

from database import get_connection


MOD_METRICS = (
    ("energy", "annual_energy_consumption"),
    ("regen", "annual_regenerative_energy"),
    ("annual_cost", "annual_electricity_expenditure"),
    ("lifetime_cost", "lifetime_electricity_expenditure"),
    ("operational_co2e", "operational_co2e"),
    ("embodied_co2e", "embodied_co2e"),
)
ISO_FIELDS = frozenset({
    "annual_energy_kwh", "annual_energy_wh", "efficiency_class",
})


STATUS_SQL = """
    SELECT m.case_ref, m.status AS case_status,
           d.document_id, d.filename, d.source_classification,
           d.revision_no, d.review_scope, d.review_status
    FROM modernisation_cases m
    LEFT JOIN LATERAL (
        SELECT p.document_id, p.filename, p.source_classification,
               r.revision_no, r.review_scope, r.review_status
        FROM project_documents p
        LEFT JOIN LATERAL (
            SELECT revision_no, review_scope, review_status
            FROM document_revisions
            WHERE document_id = p.document_id
            ORDER BY revision_no DESC LIMIT 1
        ) r ON TRUE
        WHERE p.case_id = m.case_id AND p.document_type = 'mod_pdf'
        ORDER BY p.document_id DESC LIMIT 1
    ) d ON TRUE
    ORDER BY m.case_ref
"""

MOD_SQL = """
    SELECT d.document_id, m.case_ref, d.filename, d.source_classification,
           r.revision_id, r.revision_no, r.review_scope, r.review_status,
           v.phase, v.field_name, v.numeric_value, v.text_value,
           v.raw_source_value, v.unit, v.source_verified
    FROM project_documents d
    JOIN modernisation_cases m ON m.case_id = d.case_id
    JOIN document_revisions r ON r.document_id = d.document_id
    LEFT JOIN engineering_values v ON v.revision_id = r.revision_id
    WHERE d.document_type = 'mod_pdf'
      AND d.document_id = (
          SELECT MAX(newest.document_id)
          FROM project_documents newest
          WHERE newest.case_id = d.case_id AND newest.document_type = 'mod_pdf'
      )
      AND r.revision_no = (
          SELECT MAX(latest.revision_no)
          FROM document_revisions latest WHERE latest.document_id = d.document_id
      )
      AND r.review_status = 'approved'
    ORDER BY d.document_id DESC, v.phase, v.field_name
"""

ISO_SQL = """
    SELECT m.case_ref, d.document_id, d.filename, d.reported_elevator_ref,
           d.uploaded_at, d.source_classification,
           r.revision_no, r.review_scope, r.review_status,
           v.phase, v.field_name, v.numeric_value, v.text_value
    FROM project_documents d
    JOIN modernisation_cases m ON m.case_id = d.case_id
    LEFT JOIN LATERAL (
        SELECT revision_id, revision_no, review_scope, review_status
        FROM document_revisions
        WHERE document_id = d.document_id
        ORDER BY revision_no DESC LIMIT 1
    ) r ON TRUE
    LEFT JOIN engineering_values v ON v.revision_id = r.revision_id
        AND v.field_name = ANY(%s)
    WHERE d.document_type = 'iso_excel'
      AND d.document_id = (
          SELECT MAX(newest.document_id)
          FROM project_documents newest
          WHERE newest.case_id = d.case_id AND newest.document_type = 'iso_excel'
      )
    ORDER BY m.case_ref, v.phase, v.field_name
"""


def _rows(cursor):
    names = [item.name for item in cursor.description]
    return [dict(zip(names, row, strict=True)) for row in cursor.fetchall()]


def _read(query, params=None):
    with get_connection() as connection, connection.cursor() as cursor:
        cursor.execute("SET TRANSACTION READ ONLY")
        cursor.execute("SET LOCAL statement_timeout = '15s'")
        cursor.execute(query, params) if params is not None else cursor.execute(query)
        return _rows(cursor)


def _number(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        result = Decimal(str(value))
    except (ValueError, TypeError, InvalidOperation):
        return None
    return result if result.is_finite() else None


def _field_value(item):
    if item is None:
        return None
    return item["numeric_value"] if item["numeric_value"] is not None else item["text_value"]


def _field_text(item):
    if item is None:
        return None
    value = _field_value(item)
    if value is None:
        value = item.get("raw_source_value")
    return str(value).strip() if value is not None else None


def _lookup(fields, phase, name):
    return fields.get((phase, name))


def _annual_cost_currency(fields):
    existing = _lookup(fields, "existing", "annual_electricity_expenditure")
    proposed = _lookup(fields, "proposed", "annual_electricity_expenditure")
    units = [str(item.get("unit") or "") for item in (existing, proposed) if item]
    if len(units) != 2 or units[0] != units[1] or not units[0].endswith("/year"):
        return ""
    return units[0].removesuffix("/year")


def _lifetime(fields):
    value = _lookup(fields, "common", "lifetime_years")
    number = _number(_field_value(value))
    if number is not None and number > 0 and number == number.to_integral_value():
        return int(number)
    for phase in ("existing", "proposed"):
        operational = _lookup(fields, phase, "operational_co2e")
        unit = str((operational or {}).get("unit") or "")
        match = re.search(r"lifetime\s+(\d+)\s+years", unit, flags=re.I)
        if match:
            return int(match.group(1))
    return None


def _complete_mod(row, fields):
    """Assemble one current approved snapshot; incomplete sources are excluded."""
    source = {
        "document_id": row["document_id"],
        "case_ref": row["case_ref"],
        "filename": row["filename"],
        "revision_id": row["revision_id"],
        "revision_no": row["revision_no"],
        "review_scope": row["review_scope"],
        "source_classification": row["source_classification"],
        "country": _field_text(_lookup(fields, "common", "country")),
        "report_date": _field_text(_lookup(fields, "common", "report_date")),
        "lifetime_years": _lifetime(fields),
        "currency_symbol": _annual_cost_currency(fields),
        "electricity_price": _number(_field_value(_lookup(fields, "common", "electricity_price"))),
        "electricity_price_unit": (_lookup(fields, "common", "electricity_price") or {}).get("unit") or "",
        "reported_saving": _number(_field_value(_lookup(fields, "common", "reported_energy_saving_percent"))),
        "calculated_saving": _number(_field_value(_lookup(fields, "common", "calculated_energy_saving_percent"))),
        "total_value_count": len(fields),
        "verified_value_count": sum(bool(v["source_verified"]) for v in fields.values()),
    }
    for phase in ("existing", "proposed"):
        tech = _field_text(_lookup(fields, phase, "elevator_technology"))
        if not tech or tech.upper() in {"N/A", "NA"}:
            return None
        source[f"{phase}_technology"] = tech
        for label, field in MOD_METRICS:
            source[f"{phase}_{label}"] = _number(_field_value(_lookup(fields, phase, field)))

    old = source["existing_energy"]
    new = source["proposed_energy"]
    if old is None or old <= 0 or new is None or new < 0:
        return None

    calculated = source["calculated_saving"]
    recomputed = (old - new) * Decimal("100") / old
    if calculated is None or abs(calculated - recomputed) > Decimal("0.15"):
        # An inconsistent figure must not be presented as an approved KPI.
        return None

    source["fully_source_verified"] = (
        source["review_scope"] == "full_values"
        and source["total_value_count"] > 0
        and source["verified_value_count"] == source["total_value_count"]
    )
    source["real_verified"] = (
        source["fully_source_verified"]
        and source["source_classification"] == "real"
    )
    return source


def load_case_statuses():
    """All registered cases, with latest unified MOD PDF if present."""
    return _read(STATUS_SQL)


def load_reports():
    """Latest approved MOD revision per case, including labelled fixtures.

    A newer unapproved revision or newer MOD PDF suppresses any older approval.
    """
    rows = _read(MOD_SQL)
    grouped = {}
    for row in rows:
        report = grouped.setdefault(row["document_id"], {"header": row, "fields": {}})
        if row["field_name"] is None:
            continue
        key = (row["phase"], row["field_name"])
        if key in report["fields"]:
            raise ValueError(f"Duplicate MOD engineering value for {row['document_id']}: {key}")
        report["fields"][key] = row

    result = []
    for report in grouped.values():
        assembled = _complete_mod(report["header"], report["fields"])
        if assembled is not None:
            result.append(assembled)
    return result


def load_iso_reports():
    """Latest unified ISO workbook per case, never older approved records."""
    rows = _read(ISO_SQL, (list(sorted(ISO_FIELDS)),))
    grouped = {}
    for row in rows:
        case = row["case_ref"]
        record = grouped.setdefault(case, {
            "case_ref": case,
            "document_id": row["document_id"],
            "lift_id": row["reported_elevator_ref"],
            "imported_at": row["uploaded_at"],
            "review_status": row["review_status"] or "pending",
            "review_scope": row["review_scope"],
            "revision_no": row["revision_no"],
            "source_classification": row["source_classification"],
            "filename": row["filename"], "values": {},
        })
        if row["field_name"] is None:
            continue
        phase = row["phase"]
        if phase not in {"existing", "proposed", "common"}:
            raise ValueError("Invalid ISO engineering phase")
        prefix = {"existing": "before_", "proposed": "after_", "common": ""}[phase]
        name = prefix + row["field_name"]
        if name in record["values"]:
            raise ValueError(f"Duplicate ISO result: {case} / {name}")
        record["values"][name] = _field_value(row)
    return list(grouped.values())


def iso_annual_energy(report, role=""):
    """Use kWh when supplied; otherwise convert Wh to kWh."""
    values = report["values"]
    kwh = values.get(f"{role}annual_energy_kwh")
    wh = values.get(f"{role}annual_energy_wh")
    number = _number(kwh) if kwh is not None else (
        _number(wh) / Decimal("1000") if _number(wh) is not None else None
    )
    return number if number is not None and number >= 0 else None


def iso_saving(report):
    before = iso_annual_energy(report, "before_")
    after = iso_annual_energy(report, "after_")
    if before is None or before <= 0 or after is None:
        return None
    return (before - after) * 100 / before


def case_readiness(statuses, iso_reports):
    """Both latest comparisons approved, not equivalent to source verification."""
    by_case = {record["case_ref"]: record for record in iso_reports}
    return {
        record["case_ref"]: (
            record["review_status"] == "approved"
            and by_case.get(record["case_ref"], {}).get("review_status") == "approved"
        )
        for record in statuses
    }
