
import unittest
from datetime import date
from types import SimpleNamespace
from unittest.mock import patch

from services import measurement_pair_repository as repo


class FakeCursor:
    # Return controlled query results while recording SQL and parameters.
    def __init__(self, answers=(), rows=(), columns=(), update_count=1):
        self.answers = list(answers)
        self.rows = list(rows)
        self.description = [SimpleNamespace(name=name) for name in columns]
        self.update_count = update_count
        self.rowcount = 0
        self.queries = []

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def execute(self, query, params=()):
        self.queries.append((query, params))
        self.rowcount = (
            self.update_count
            if "UPDATE verified_measurement_pairs" in query
            else 1
        )

    def fetchone(self):
        return self.answers.pop(0) if self.answers else None

    def fetchall(self):
        return self.rows


class FakeConnection:
    # Mimic transaction commit and rollback on context exit.
    def __init__(self, answers=(), rows=(), columns=(), update_count=1):
        self.cur = FakeCursor(answers, rows, columns, update_count)
        self.committed = False
        self.rolled_back = False

    def __enter__(self):
        return self

    def __exit__(self, exc_type, *_):
        self.committed = exc_type is None
        self.rolled_back = exc_type is not None
        return False

    def cursor(self):
        return self.cur


def observation(observation_id, source_id, start, end):
    # Use fictional meter totals and dates for software tests only.
    return {
        "observation_id": observation_id,
        "source_id": source_id,
        "metric_name": "period_energy",
        "numeric_value": "240" if observation_id == 10 else "120",
        "unit": "kWh",
        "value_kind": "measured",
        "review_status": "approved",
        "verified_elevator_id": 7,
        "period_start": start,
        "period_end": end,
    }


BEFORE = observation(
    10, 101, date(2025, 1, 1), date(2025, 1, 30)
)
AFTER = observation(
    20, 102, date(2025, 3, 1), date(2025, 3, 30)
)


class TestMeasurementValidation(unittest.TestCase):
    def test_valid_observations(self):
        self.assertEqual(
            repo._check_observations(BEFORE, AFTER, 7),
            (30, 30),
        )

    def test_mismatched_elevator(self):
        invalid = {**AFTER, "verified_elevator_id": 8}

        with self.assertRaisesRegex(ValueError, "not linked"):
            repo._check_observations(BEFORE, invalid, 7)

    def test_calculated_values_are_rejected(self):
        invalid = {**AFTER, "value_kind": "calculated"}

        with self.assertRaisesRegex(ValueError, "Calculated"):
            repo._check_observations(BEFORE, invalid, 7)

    def test_wrong_units_and_sources(self):
        invalid_rows = (
            {**AFTER, "unit": "Wh"},
            {**AFTER, "source_id": 101},
        )

        for invalid in invalid_rows:
            with self.subTest(invalid=invalid):
                with self.assertRaises(ValueError):
                    repo._check_observations(BEFORE, invalid, 7)

    def test_invalid_periods(self):
        invalid = {
            **AFTER,
            "period_start": date(2025, 1, 20),
        }

        with self.assertRaises(ValueError):
            repo._check_observations(BEFORE, invalid, 7)

    def test_missing_observation_is_rejected(self):
        columns = tuple(BEFORE.keys())
        conn = FakeConnection(
            rows=[tuple(BEFORE.values())],
            columns=columns,
        )

        with self.assertRaisesRegex(ValueError, "do not exist"):
            repo._load_observations(conn.cur, 10, 20)

        self.assertIn(
            "FOR SHARE OF o, s",
            conn.cur.queries[0][0],
        )


class TestPairSubmissions(unittest.TestCase):
    def create(self, previous, pair_id=55):
        conn = FakeConnection(
            answers=[
                (7, "completed"),
                previous,
                (pair_id,),
            ]
        )

        with (
            patch.object(repo, "get_connection", return_value=conn),
            patch.object(
                repo,
                "_load_observations",
                return_value=(BEFORE, AFTER),
            ),
        ):
            result = repo.create_measurement_pair(
                2, 3, 10, 20, "Checked source periods"
            )

        return conn, result

    def test_first_submission(self):
        conn, result = self.create(None)

        self.assertEqual(result, 55)
        self.assertTrue(conn.committed)

        query, params = conn.cur.queries[-1]

        self.assertIn("submission_note", query)
        self.assertEqual(params[2], 1)
        self.assertIn(
            "FOR UPDATE OF m",
            conn.cur.queries[0][0],
        )

    def test_resubmission_after_rejection(self):
        conn, _ = self.create((1, "rejected"))

        self.assertEqual(
            conn.cur.queries[-1][1][2],
            2,
        )

    def test_pending_and_approved_block_resubmission(self):
        for status in ("pending", "approved"):
            with self.subTest(status=status):
                conn = FakeConnection(
                    answers=[
                        (7, "completed"),
                        (2, status),
                    ]
                )

                with patch.object(
                    repo, "get_connection", return_value=conn
                ):
                    with self.assertRaisesRegex(
                        ValueError, "active pair"
                    ):
                        repo.create_measurement_pair(
                            2, 3, 10, 20, "Reason"
                        )

                self.assertTrue(conn.rolled_back)
                self.assertFalse(
                    any(
                        "INSERT INTO" in query
                        for query, _ in conn.cur.queries
                    )
                )

    def test_incomplete_case_cannot_submit(self):
        conn = FakeConnection(
            answers=[(7, "registered")]
        )

        with patch.object(
            repo, "get_connection", return_value=conn
        ):
            with self.assertRaisesRegex(ValueError, "completed"):
                repo.create_measurement_pair(
                    2, 3, 10, 20, "Reason"
                )

        self.assertTrue(conn.rolled_back)

    def test_note_required(self):
        with self.assertRaisesRegex(ValueError, "Explain"):
            repo.create_measurement_pair(
                2, 3, 10, 20, "  "
            )


class TestPairReviews(unittest.TestCase):
    pair = ("pending", 7, 10, 20, 30, 30)

    def review(self, decision, answers, update_count=1, **options):
        conn = FakeConnection(
            answers=answers,
            update_count=update_count,
        )

        with (
            patch.object(repo, "get_connection", return_value=conn),
            patch.object(
                repo,
                "_load_observations",
                return_value=(BEFORE, AFTER),
            ),
        ):
            result = repo.review_measurement_pair(
                55, decision, "Reviewed", **options
            )

        return conn, result

    def test_approved_pair_passes_eligibility_view(self):
        conn, result = self.review(
            "approved",
            [self.pair, (1,)],
            conditions_verified=True,
            period_energy_verified=True,
        )

        self.assertEqual(result, "approved")
        self.assertTrue(conn.committed)
        self.assertTrue(
            any(
                "ai_verified_measurement_pairs_view" in query
                for query, _ in conn.cur.queries
            )
        )

    def test_eligibility_failure_rolls_back(self):
        conn = FakeConnection(
            answers=[self.pair, None]
        )

        with (
            patch.object(repo, "get_connection", return_value=conn),
            patch.object(
                repo,
                "_load_observations",
                return_value=(BEFORE, AFTER),
            ),
        ):
            with self.assertRaisesRegex(
                ValueError, "eligibility"
            ):
                repo.review_measurement_pair(
                    55,
                    "approved",
                    "Reviewed",
                    conditions_verified=True,
                    period_energy_verified=True,
                )

        self.assertTrue(conn.rolled_back)

    def test_rejection_does_not_require_measurement_loading(self):
        conn = FakeConnection(
            answers=[self.pair]
        )

        with (
            patch.object(repo, "get_connection", return_value=conn),
            patch.object(repo, "_load_observations") as loader,
        ):
            result = repo.review_measurement_pair(
                55, "rejected", "Reason"
            )

        self.assertEqual(result, "rejected")
        loader.assert_not_called()
        self.assertTrue(conn.committed)

    def test_approval_requires_both_confirmations(self):
        with self.assertRaisesRegex(ValueError, "Confirm"):
            repo.review_measurement_pair(
                55,
                "approved",
                "Reason",
                conditions_verified=True,
            )

    def test_concurrent_review_update_is_rejected(self):
        conn = FakeConnection(
            answers=[self.pair],
            update_count=0,
        )

        with patch.object(
            repo, "get_connection", return_value=conn
        ):
            with self.assertRaisesRegex(
                RuntimeError, "changed during review"
            ):
                repo.review_measurement_pair(
                    55, "rejected", "Reason"
                )

        self.assertTrue(conn.rolled_back)

    def test_review_history_query(self):
        columns = (
            "pair_id",
            "revision_no",
            "review_status",
        )

        conn = FakeConnection(
            rows=[
                (55, 2, "pending"),
                (40, 1, "rejected"),
            ],
            columns=columns,
        )

        with patch.object(
            repo, "get_connection", return_value=conn
        ):
            rows = repo.list_measurement_pair_revisions(2)

        self.assertEqual(
            [row["revision_no"] for row in rows],
            [2, 1],
        )

        self.assertIn(
            "ORDER BY revision_no DESC",
            conn.cur.queries[0][0],
        )


if __name__ == "__main__":
    unittest.main()
