"""Read-only measured-vs-estimated energy savings for KONE modernisation.

Only approved, eligible meter-reading pairs from migration 010 are used for
measured outcomes. Optional EnerCal estimates are joined by the exact PDF ID
and remain separate from the measured values. No writes, schema changes or
synthetic fallback measurements are performed.
"""

from datetime import date
from decimal import Decimal, InvalidOperation
import sys
import unittest


# The eligibility view enforces source approval, actual metered kWh, matching
# elevators, nonoverlapping before/after periods and a completed case.
# Two EnerCal roles are required; alternative proposals are not aggregated.
REPORT_QUERY = """
SELECT
    v.pair_id,
    v.case_id,
    m.case_ref,
    v.document_id,
    v.elevator_id,
    v.before_source_id,
    v.after_source_id,
    v.before_kwh,
    v.after_kwh,
    v.before_days,
    v.after_days,
    b.period_start AS before_start,
    b.period_end AS before_end,
    a.period_start AS after_start,
    a.period_end AS after_end,
    estimate.estimated_before_kwh_year,
    estimate.estimated_after_kwh_year,
    estimate.enercal_saving_percent
FROM ai_verified_measurement_pairs_view v
JOIN modernisation_cases m ON m.case_id = v.case_id
JOIN verified_measurement_pairs p ON p.pair_id = v.pair_id
JOIN energy_observations b ON b.observation_id = p.before_observation_id
JOIN energy_observations a ON a.observation_id = p.after_observation_id
LEFT JOIN LATERAL (
    SELECT
        MAX(k.annual_energy_kwh_year) FILTER (
            WHERE k.technology_role = 'existing'
        ) AS estimated_before_kwh_year,
        MAX(k.annual_energy_kwh_year) FILTER (
            WHERE k.technology_role = 'proposed'
        ) AS estimated_after_kwh_year,
        MAX(k.calculated_energy_saving_percent) AS enercal_saving_percent
    FROM enercal_approved_kpis_view k
    WHERE k.document_id = v.document_id
    HAVING COUNT(*) = 2
       AND COUNT(*) FILTER (WHERE k.technology_role = 'existing') = 1
       AND COUNT(*) FILTER (WHERE k.technology_role = 'proposed') = 1
) estimate ON TRUE
ORDER BY m.case_ref, v.pair_id
"""


def _decimal(value, label):
    try:
        number = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"{label} is not a valid number.") from exc
    if not number.is_finite():
        raise ValueError(f"{label} must be finite.")
    return number


def _days(value, label):
    if isinstance(value, bool):
        raise ValueError(f"{label} must be a positive integer.")
    try:
        number = int(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{label} must be a positive integer.") from exc
    if number < 1 or str(value).strip() != str(number):
        raise ValueError(f"{label} must be a positive integer.")
    return number


def _date(value, label):
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} is not a valid date.") from exc


def calculate_verified_savings(row):
    """Convert period meter totals to daily rates before comparing them.

    Returns a new dictionary without modifying the database row. A negative
    saving is valid: it means the observed daily rate increased after works.
    """
    pair_id = row.get("pair_id")
    before = _decimal(row.get("before_kwh"), "before_kwh")
    after = _decimal(row.get("after_kwh"), "after_kwh")
    before_days = _days(row.get("before_days"), "before_days")
    after_days = _days(row.get("after_days"), "after_days")

    if before <= 0 or after < 0:
        raise ValueError(f"Pair {pair_id}: invalid measured energy totals.")

    before_start = _date(row.get("before_start"), "before_start")
    before_end = _date(row.get("before_end"), "before_end")
    after_start = _date(row.get("after_start"), "after_start")
    after_end = _date(row.get("after_end"), "after_end")
    if (before_end - before_start).days + 1 != before_days:
        raise ValueError(f"Pair {pair_id}: before period days do not match the dates.")
    if (after_end - after_start).days + 1 != after_days:
        raise ValueError(f"Pair {pair_id}: after period days do not match the dates.")
    if before_end >= after_start:
        raise ValueError(f"Pair {pair_id}: before and after periods overlap or are reversed.")

    daily_before = before / Decimal(before_days)
    daily_after = after / Decimal(after_days)
    measured_percent = (Decimal(1) - daily_after / daily_before) * 100

    result = dict(row)
    result.update({
        "measured_before_kwh_per_day": daily_before,
        "measured_after_kwh_per_day": daily_after,
        "measured_saving_percent": measured_percent,
        "enercal_estimated_saving_percent": None,
        "measured_minus_estimated_pp": None,
        "estimate_status": "No complete approved EnerCal comparison for this PDF",
    })

    estimated_before = row.get("estimated_before_kwh_year")
    estimated_after = row.get("estimated_after_kwh_year")
    if estimated_before is None or estimated_after is None:
        return result

    estimated_before = _decimal(estimated_before, "estimated_before_kwh_year")
    estimated_after = _decimal(estimated_after, "estimated_after_kwh_year")
    if estimated_before <= 0 or estimated_after < 0:
        result["estimate_status"] = "Approved EnerCal energy values are invalid"
        return result

    calculated_estimate = (Decimal(1) - estimated_after / estimated_before) * 100
    reported = row.get("enercal_saving_percent")
    if reported is not None:
        reported = _decimal(reported, "enercal_saving_percent")
        if abs(calculated_estimate - reported) > Decimal("0.15"):
            result["estimate_status"] = "EnerCal percentage does not match its energy values"
            return result

    result.update({
        "enercal_estimated_saving_percent": calculated_estimate,
        "measured_minus_estimated_pp": measured_percent - calculated_estimate,
        "estimate_status": "Comparable approved EnerCal estimate available",
    })
    return result


def load_verified_savings():
    """Read eligible pairs; return an empty list when none are eligible.

    The only SQL statement reading project data is a SELECT in a read-only
    transaction. Unapproved reports and unverified pairs never appear.
    """
    from database import get_connection

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SET TRANSACTION READ ONLY")
            cur.execute("SET LOCAL statement_timeout = '15s'")
            cur.execute(REPORT_QUERY)
            columns = [column.name for column in cur.description]
            rows = [dict(zip(columns, item)) for item in cur.fetchall()]

    return [calculate_verified_savings(row) for row in rows]


def _format(number):
    return "N/A" if number is None else f"{number:,.2f}"


def main():
    print("KONE | Measured versus EnerCal-estimated savings (read-only)")
    try:
        rows = load_verified_savings()
    except Exception as exc:
        print(f"ERROR: Unable to load verified measurement savings: {exc}")
        return 1

    if not rows:
        print("0 eligible verified before/after measurement pairs.")
        print("This is normal when projects are registered, incomplete, or not independently verified.")
        print("Pending EnerCal reports are not treated as measured energy savings.")
        return 0

    for row in rows:
        print(f"\n{row['case_ref']} | PDF {row['document_id']} | Pair {row['pair_id']}")
        print(f"  Metered before: {_format(row['measured_before_kwh_per_day'])} kWh/day")
        print(f"  Metered after:  {_format(row['measured_after_kwh_per_day'])} kWh/day")
        print(f"  Observed period-normalized change: {_format(row['measured_saving_percent'])}%")
        print(f"  Approved EnerCal estimate: {_format(row['enercal_estimated_saving_percent'])}%")
        print(f"  Measured minus estimated: {_format(row['measured_minus_estimated_pp'])} percentage points")
        print(f"  {row['estimate_status']}")
    print("\nThese are normalized comparisons of measured periods, not causal proof or annual meter totals.")
    return 0


class _CalculationTests(unittest.TestCase):
    def example(self, **changes):
        row = {
            "pair_id": 1, "case_ref": "EXAMPLE", "document_id": 123,
            "before_kwh": Decimal("300"), "after_kwh": Decimal("150"),
            "before_days": 30, "after_days": 30,
            "before_start": date(2025, 1, 1), "before_end": date(2025, 1, 30),
            "after_start": date(2025, 4, 1), "after_end": date(2025, 4, 30),
            "estimated_before_kwh_year": Decimal("1000"),
            "estimated_after_kwh_year": Decimal("600"),
            "enercal_saving_percent": Decimal("40"),
        }
        row.update(changes)
        return row

    def test_equal_periods_and_enercal_gap(self):
        value = calculate_verified_savings(self.example())
        self.assertEqual(value["measured_saving_percent"], Decimal("50.0"))
        self.assertEqual(value["measured_minus_estimated_pp"], Decimal("10.0"))

    def test_unequal_periods_use_daily_rates(self):
        row = self.example(
            before_kwh=Decimal("300"), before_days=30,
            after_kwh=Decimal("150"), after_days=15,
            after_end=date(2025, 4, 15),
        )
        value = calculate_verified_savings(row)
        self.assertEqual(value["measured_saving_percent"], Decimal("0"))

    def test_missing_approved_enercal_remains_missing(self):
        row = self.example(
            estimated_before_kwh_year=None,
            estimated_after_kwh_year=None,
        )
        value = calculate_verified_savings(row)
        self.assertIsNone(value["enercal_estimated_saving_percent"])
        self.assertIsNone(value["measured_minus_estimated_pp"])

    def test_invalid_dates_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "days do not match"):
            calculate_verified_savings(self.example(before_days=29))

    def test_invalid_energy_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "invalid measured energy"):
            calculate_verified_savings(self.example(after_kwh=-1))

    def test_inconsistent_enercal_is_not_compared(self):
        result = calculate_verified_savings(self.example(enercal_saving_percent=25))
        self.assertIsNone(result["measured_minus_estimated_pp"])
        self.assertIn("does not match", result["estimate_status"])

    def test_rise_in_consumption_is_negative_saving(self):
        result = calculate_verified_savings(self.example(after_kwh=450))
        self.assertEqual(result["measured_saving_percent"], Decimal("-50.0"))


if __name__ == "__main__":
    if "--self-test" in sys.argv:
        suite = unittest.defaultTestLoader.loadTestsFromTestCase(_CalculationTests)
        outcome = unittest.TextTestRunner(verbosity=2).run(suite)
        raise SystemExit(0 if outcome.wasSuccessful() else 1)
    raise SystemExit(main())
