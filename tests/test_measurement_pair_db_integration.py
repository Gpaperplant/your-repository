
"""Test measurement-pair workflows in an isolated PostgreSQL schema."""

import os
import unittest
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

import psycopg
from psycopg import sql

from services import measurement_pair_repository as repo


ROOT = Path(__file__).resolve().parents[1]
TEST_DATABASE = "request_app_test"


class TestMeasurementPairDatabase(unittest.TestCase):
    @classmethod
    def _connect(cls):
        # Guard every connection against using the project database.
        conn = psycopg.connect(
            host="127.0.0.1",
            port=5432,
            dbname=TEST_DATABASE,
            user="app_user",
            password=os.environ["POSTGRES_PASSWORD"],
        )

        database = conn.execute(
            "SELECT current_database()"
        ).fetchone()[0]

        if database != TEST_DATABASE:
            conn.close()
            raise RuntimeError(
                "Refusing to run against a non-test database."
            )

        conn.execute(
            sql.SQL("SET search_path TO {}").format(
                sql.Identifier(cls.schema)
            )
        )
        conn.commit()

        return conn

    @classmethod
    def setUpClass(cls):
        # Create a disposable schema for this test run.
        if not os.getenv("POSTGRES_PASSWORD"):
            raise RuntimeError(
                "POSTGRES_PASSWORD is required in .env."
            )

        cls.schema = "fyp_pair_test_" + uuid4().hex[:12]

        with cls._connect() as conn:
            conn.execute(
                sql.SQL("CREATE SCHEMA {}").format(
                    sql.Identifier(cls.schema)
                )
            )

        try:
            with cls._connect() as conn:
                # Create minimal source tables for migrations 010 and 011.
                conn.execute(
                    """
                    CREATE TABLE modernisation_cases (
                        case_id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                        elevator_id BIGINT NOT NULL,
                        status TEXT NOT NULL
                    );

                    CREATE TABLE source_documents (
                        document_id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                        case_id BIGINT NOT NULL
                            REFERENCES modernisation_cases(case_id)
                    );

                    CREATE TABLE measurement_sources (
                        source_id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                        verified_elevator_id BIGINT,
                        review_status TEXT NOT NULL
                    );

                    CREATE TABLE energy_observations (
                        observation_id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                        source_id BIGINT NOT NULL
                            REFERENCES measurement_sources(source_id),
                        metric_name TEXT NOT NULL,
                        numeric_value NUMERIC NOT NULL,
                        unit TEXT NOT NULL,
                        value_kind TEXT NOT NULL,
                        period_start DATE,
                        period_end DATE
                    );
                    """
                )

            for filename in (
                "010_verified_measurement_pairs.sql",
                "011_measurement_pair_revisions.sql",
            ):
                migration = (
                    ROOT / "database" / "migrations" / filename
                )

                if not migration.is_file():
                    raise FileNotFoundError(
                        f"Missing migration: {migration}"
                    )

                with cls._connect() as conn:
                    conn.execute(
                        migration.read_text(encoding="utf-8")
                    )

        except Exception:
            cls.tearDownClass()
            raise

    @classmethod
    def tearDownClass(cls):
        # Delete only the temporary schema created by this test.
        if not hasattr(cls, "schema"):
            return

        with cls._connect() as conn:
            conn.execute(
                sql.SQL(
                    "DROP SCHEMA IF EXISTS {} CASCADE"
                ).format(sql.Identifier(cls.schema))
            )

    def setUp(self):
        # Create synthetic measurements for each independent test.
        with self._connect() as conn:
            self.case_id = conn.execute(
                """
                INSERT INTO modernisation_cases
                    (elevator_id, status)
                VALUES (7, 'completed')
                RETURNING case_id
                """
            ).fetchone()[0]

            self.document_id = conn.execute(
                """
                INSERT INTO source_documents (case_id)
                VALUES (%s)
                RETURNING document_id
                """,
                (self.case_id,),
            ).fetchone()[0]

            source_ids = []

            for _ in range(2):
                source_id = conn.execute(
                    """
                    INSERT INTO measurement_sources
                        (verified_elevator_id, review_status)
                    VALUES (7, 'approved')
                    RETURNING source_id
                    """
                ).fetchone()[0]

                source_ids.append(source_id)

            self.before_id = conn.execute(
                """
                INSERT INTO energy_observations (
                    source_id, metric_name, numeric_value,
                    unit, value_kind, period_start, period_end
                )
                VALUES (
                    %s, 'period_energy', 300,
                    'kWh', 'measured', '2025-01-01', '2025-01-30'
                )
                RETURNING observation_id
                """,
                (source_ids[0],),
            ).fetchone()[0]

            self.after_id = conn.execute(
                """
                INSERT INTO energy_observations (
                    source_id, metric_name, numeric_value,
                    unit, value_kind, period_start, period_end
                )
                VALUES (
                    %s, 'period_energy', 150,
                    'kWh', 'measured', '2025-04-01', '2025-04-30'
                )
                RETURNING observation_id
                """,
                (source_ids[1],),
            ).fetchone()[0]

        # Redirect repository database operations into the test schema.
        connection_patch = patch.object(
            repo,
            "get_connection",
            self._connect,
        )

        connection_patch.start()
        self.addCleanup(connection_patch.stop)

    def _submit(self):
        # Reuse the actual repository function being tested.
        return repo.create_measurement_pair(
            self.case_id,
            self.document_id,
            self.before_id,
            self.after_id,
            "Synthetic test period comparison",
        )

    def test_reject_resubmit_approve_and_history(self):
        # Verify that rejected revisions remain available.
        first = self._submit()

        repo.review_measurement_pair(
            first,
            "rejected",
            "Incorrect measurement period",
        )

        second = self._submit()

        self.assertNotEqual(first, second)

        repo.review_measurement_pair(
            second,
            "approved",
            "Synthetic test measurements verified",
            conditions_verified=True,
            period_energy_verified=True,
        )

        history = repo.list_measurement_pair_revisions(
            self.case_id
        )

        self.assertEqual(
            [
                (row["revision_no"], row["review_status"])
                for row in history
            ],
            [
                (2, "approved"),
                (1, "rejected"),
            ],
        )

        self.assertIn(
            "Synthetic test",
            history[0]["submission_note"],
        )

        with self._connect() as conn:
            approved = conn.execute(
                """
                SELECT pair_id
                FROM ai_verified_measurement_pairs_view
                WHERE case_id = %s
                """,
                (self.case_id,),
            ).fetchall()

        self.assertEqual(approved, [(second,)])

    def test_active_pair_blocks_duplicate_submission(self):
        # Prevent multiple active revisions for one case.
        self._submit()

        with self.assertRaisesRegex(
            ValueError,
            "active pair",
        ):
            self._submit()

        with self._connect() as conn:
            count = conn.execute(
                """
                SELECT COUNT(*)
                FROM verified_measurement_pairs
                WHERE case_id = %s
                """,
                (self.case_id,),
            ).fetchone()[0]

        self.assertEqual(count, 1)

    def test_invalid_source_cannot_be_submitted(self):
        # Reject calculated energy even when the source is approved.
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE energy_observations
                SET value_kind = 'calculated'
                WHERE observation_id = %s
                """,
                (self.after_id,),
            )

        with self.assertRaisesRegex(
            ValueError,
            "Calculated",
        ):
            self._submit()

    def test_approval_failure_rolls_back(self):
        # Confirm that failed eligibility does not save approval.
        pair_id = self._submit()

        with self._connect() as conn:
            other_case = conn.execute(
                """
                INSERT INTO modernisation_cases
                    (elevator_id, status)
                VALUES (7, 'completed')
                RETURNING case_id
                """
            ).fetchone()[0]

            conn.execute(
                """
                UPDATE source_documents
                SET case_id = %s
                WHERE document_id = %s
                """,
                (other_case, self.document_id),
            )

        with self.assertRaisesRegex(
            ValueError,
            "eligibility",
        ):
            repo.review_measurement_pair(
                pair_id,
                "approved",
                "Synthetic approval test",
                conditions_verified=True,
                period_energy_verified=True,
            )

        with self._connect() as conn:
            status = conn.execute(
                """
                SELECT review_status
                FROM verified_measurement_pairs
                WHERE pair_id = %s
                """,
                (pair_id,),
            ).fetchone()[0]

        self.assertEqual(status, "pending")


if __name__ == "__main__":
    unittest.main()
