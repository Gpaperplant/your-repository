
from collections import Counter
from math import isfinite

from database import get_connection


# Use only values available before modernisation as model inputs.
FEATURE_COLUMNS = (
    "predicted_saving_percent",
    "estimated_before_kwh_year",
    "estimated_after_kwh_year",
    "nominal_load_kg",
    "existing_speed_m_s",
    "proposed_speed_m_s",
    "travel_distance_m",
    "served_floors",
    "starts_per_year",
    "operating_days_per_year",
    "iso_usage_category",
)

# Map approved EnerCal specification keys to model input names.
SPEC_FIELDS = {
    "existing_nominal_load_kg": "nominal_load_kg",
    "existing_nominal_speed_m_s": "existing_speed_m_s",
    "proposed_nominal_speed_m_s": "proposed_speed_m_s",
    "travel_distance_m": "travel_distance_m",
    "served_floors": "served_floors",
    "starts_per_year": "starts_per_year",
    "operating_days_per_year": "operating_days_per_year",
    "iso_usage_category": "iso_usage_category",
}


def _number(value):
    # Reject missing, nonnumeric and nonfinite values.
    if value is None:
        return None
    try:
        result = float(str(value).strip().replace(",", ""))
    except (TypeError, ValueError, OverflowError):
        return None
    return result if isfinite(result) else None


def _rows(cursor):
    # Return query results as dictionaries without a Pandas dependency.
    columns = [column.name for column in cursor.description]
    return [dict(zip(columns, row)) for row in cursor.fetchall()]


def load_approved_enercal(document_ids):
    # Only completed projects with a current approved EnerCal revision qualify.
    if not document_ids:
        return {}

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    v.document_id,
                    d.case_id,
                    m.case_ref,
                    m.elevator_id,
                    MAX(v.annual_energy_consumption_kwh_year)
                        FILTER (WHERE v.technology_role = 'existing')
                        AS estimated_before_kwh_year,
                    MAX(v.annual_energy_consumption_kwh_year)
                        FILTER (WHERE v.technology_role = 'proposed')
                        AS estimated_after_kwh_year,
                    MAX(v.calculated_energy_saving_percent)
                        AS predicted_saving_percent
                FROM enercal_approved_comparison_view v
                JOIN source_documents d
                    ON d.document_id = v.document_id
                JOIN modernisation_cases m
                    ON m.case_id = d.case_id
                WHERE m.status = 'completed'
                  AND v.document_id = ANY(%s)
                GROUP BY
                    v.document_id,
                    d.case_id,
                    m.case_ref,
                    m.elevator_id
                HAVING COUNT(*) = 2
                   AND COUNT(*) FILTER (
                       WHERE v.technology_role = 'existing'
                   ) = 1
                   AND COUNT(*) FILTER (
                       WHERE v.technology_role = 'proposed'
                   ) = 1
                """,
                (document_ids,),
            )

            reports = {
                row["document_id"]: row
                for row in _rows(cur)
            }

            if not reports:
                return {}

            cur.execute(
                """
                SELECT document_id, field_name, field_value
                FROM enercal_specifications
                WHERE document_id = ANY(%s)
                  AND field_name = ANY(%s)
                """,
                (list(reports), list(SPEC_FIELDS)),
            )
            specifications = _rows(cur)

    for report in reports.values():
        for name in FEATURE_COLUMNS:
            if name not in report:
                report[name] = None

        for name in (
            "estimated_before_kwh_year",
            "estimated_after_kwh_year",
            "predicted_saving_percent",
        ):
            report[name] = _number(report[name])

    for item in specifications:
        name = SPEC_FIELDS[item["field_name"]]
        reports[item["document_id"]][name] = _number(
            item["field_value"]
        )

    return reports


def _verified_measurements(pair, report):
    # Never treat an ISO calculation as actual pre/post meter consumption.
    if pair.get("review_status") != "approved":
        raise ValueError(
            "Measurement pair is not approved."
        )

    if (
        pair.get("before_basis") != "metered"
        or pair.get("after_basis") != "metered"
    ):
        raise ValueError(
            "Both energy totals must be from meter readings."
        )

    if (
        pair.get("before_unit") != "kWh"
        or pair.get("after_unit") != "kWh"
    ):
        raise ValueError(
            "Both meter readings must be in kWh."
        )

    if pair.get("conditions_verified") is not True:
        raise ValueError(
            "Comparable operating conditions are not verified."
        )

    if (
        pair.get("case_id") != report["case_id"]
        or pair.get("elevator_id") != report["elevator_id"]
    ):
        raise ValueError(
            "Measurement case and elevator do not match EnerCal."
        )

    before_source = pair.get("before_source_id")
    after_source = pair.get("after_source_id")

    if (
        before_source is None
        or after_source is None
        or before_source == after_source
    ):
        raise ValueError(
            "Distinct before and after measurement sources are required."
        )

    before_kwh = _number(pair.get("before_kwh"))
    after_kwh = _number(pair.get("after_kwh"))
    before_days = _number(pair.get("before_days"))
    after_days = _number(pair.get("after_days"))

    if (
        before_kwh is None
        or before_kwh <= 0
        or after_kwh is None
        or after_kwh < 0
        or before_days is None
        or before_days <= 0
        or after_days is None
        or after_days <= 0
    ):
        raise ValueError(
            "Invalid metered energy or measurement period."
        )

    # Compare daily consumption rates when periods have different lengths.
    return (
        before_kwh / before_days,
        after_kwh / after_days,
    )


def build_ai_dataset(measurement_pairs):
    # Match each reviewed measurement pair to its approved EnerCal document.
    pairs = list(measurement_pairs)

    if not pairs:
        return [], []

    counts = Counter(
        pair.get("case_id")
        for pair in pairs
    )

    document_ids = list({
        pair.get("document_id")
        for pair in pairs
        if type(pair.get("document_id")) is int
    })

    reports = load_approved_enercal(document_ids)
    dataset = []
    excluded = []

    for pair in pairs:
        document_id = pair.get("document_id")

        try:
            if counts[pair.get("case_id")] != 1:
                raise ValueError(
                    "Multiple measurement pairs for one case."
                )

            report = reports.get(document_id)

            if report is None:
                raise ValueError(
                    "No approved EnerCal report for a completed case."
                )

            before = report["estimated_before_kwh_year"]
            after = report["estimated_after_kwh_year"]
            predicted = report["predicted_saving_percent"]

            if (
                before is None
                or before <= 0
                or after is None
                or after < 0
            ):
                raise ValueError(
                    "EnerCal annual energy values are invalid."
                )

            calculated = 100 * (before - after) / before

            if (
                predicted is None
                or abs(predicted - calculated) > 0.15
            ):
                raise ValueError(
                    "EnerCal predicted saving does not match its energy values."
                )

            before_daily, after_daily = _verified_measurements(
                pair,
                report,
            )

            measured = (
                100 * (before_daily - after_daily) / before_daily
            )

            features = {
                name: report[name]
                for name in FEATURE_COLUMNS
            }

            # Migration 007 specifications do not have individual approval status.
            if pair.get("specifications_verified") is not True:
                for name in SPEC_FIELDS.values():
                    features[name] = None

            dataset.append({
                "document_id": document_id,
                "case_id": report["case_id"],
                "case_ref": report["case_ref"],
                "elevator_id": report["elevator_id"],
                "before_source_id": pair["before_source_id"],
                "after_source_id": pair["after_source_id"],
                **features,
                "measured_before_kwh_per_day": round(
                    before_daily, 6
                ),
                "measured_after_kwh_per_day": round(
                    after_daily, 6
                ),
                "measured_saving_percent": round(
                    measured, 4
                ),
                "target_correction_pp": round(
                    measured - predicted, 4
                ),
            })

        except ValueError as exc:
            excluded.append({
                "document_id": document_id,
                "reason": str(exc),
            })

    return dataset, excluded


def model_inputs(dataset):
    # Exclude measured outcomes and identifiers from model input features.
    features = [
        {
            name: row[name]
            for name in FEATURE_COLUMNS
        }
        for row in dataset
    ]

    targets = [
        row["target_correction_pp"]
        for row in dataset
    ]

    groups = [
        row["elevator_id"]
        for row in dataset
    ]

    return features, targets, groups
