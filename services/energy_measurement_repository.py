
"""Store and review independent ISO measurement sources."""

import hashlib
from decimal import Decimal

from database import get_connection
from services.energy_measurement_extractor import MAX_SIZE, extract_measurements


def _rows(cur):
    columns = [column.name for column in cur.description]
    return [dict(zip(columns, row)) for row in cur.fetchall()]


def find_existing_measurement(file_bytes):
    checksum = hashlib.sha256(file_bytes).hexdigest()

    with get_connection() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT source_id FROM measurement_sources WHERE file_sha256 = %s",
            (checksum,),
        )
        row = cur.fetchone()

    return row[0] if row else None


def save_measurement_source(filename, file_bytes, extracted=None):
    if not file_bytes or len(file_bytes) > MAX_SIZE:
        raise ValueError("Measurement file is empty or too large")

    # Reuse extracted results when provided by the upload page
    data = extracted if extracted is not None else extract_measurements(
        file_bytes, filename
    )

    source_type = data["source_type"]
    reference = data["reported_elevator_ref"]
    observations = data["observations"]

    if source_type not in {"iso_excel", "iso_test_pdf"} or not reference or not observations:
        raise ValueError("Invalid measurement extraction")

    extension = {"iso_excel": ".xlsx", "iso_test_pdf": ".pdf"}[source_type]
    if not filename.lower().endswith(extension):
        raise ValueError("File extension does not match the measurement source")

    identifiers = [
        (item["metric_name"], item["source_location"])
        for item in observations
    ]

    if len(identifiers) != len(set(identifiers)):
        raise ValueError("Repeated observation locations in source")

    if any(
        not isinstance(item["numeric_value"], Decimal)
        or not item["numeric_value"].is_finite()
        or item["numeric_value"] < 0
        for item in observations
    ):
        raise ValueError("Invalid measurement values")

    checksum = hashlib.sha256(file_bytes).hexdigest()

    # Store the source and observations in one transaction
    with get_connection() as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO measurement_sources
                (filename, source_type, file_sha256, file_size_bytes,
                 file_bytes, reported_elevator_ref)
            VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT (file_sha256) DO NOTHING
            RETURNING source_id
            """,
            (
                filename,
                source_type,
                checksum,
                len(file_bytes),
                file_bytes,
                reference,
            ),
        )

        saved = cur.fetchone()

        if saved is None:
            raise ValueError("This measurement file is already saved")

        source_id = saved[0]

        cur.executemany(
            """
            INSERT INTO energy_observations
                (source_id, metric_name, raw_value, numeric_value, unit,
                 value_kind, source_location, observed_on,
                 period_start, period_end)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            [
                (
                    source_id,
                    item["metric_name"],
                    item["raw_value"],
                    item["numeric_value"],
                    item["unit"],
                    item["value_kind"],
                    item["source_location"],
                    item["observed_on"],
                    item["period_start"],
                    item["period_end"],
                )
                for item in observations
            ],
        )

    return {
        "source_id": source_id,
        "observations_saved": len(observations),
    }


def get_measurement_sources():
    with get_connection() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT
                s.source_id,
                s.filename,
                s.source_type,
                s.reported_elevator_ref,
                s.review_status,
                s.uploaded_at,
                e.elevator_ref AS verified_elevator_ref,
                COUNT(o.observation_id) AS observation_count
            FROM measurement_sources s
            LEFT JOIN elevators e
                ON e.elevator_id = s.verified_elevator_id
            LEFT JOIN energy_observations o
                ON o.source_id = s.source_id
            GROUP BY s.source_id, e.elevator_ref
            ORDER BY s.source_id DESC
            """
        )

        return _rows(cur)


def get_measurement_details(source_id):
    with get_connection() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT
                s.*,
                e.elevator_ref AS verified_elevator_ref
            FROM measurement_sources s
            LEFT JOIN elevators e
                ON e.elevator_id = s.verified_elevator_id
            WHERE s.source_id = %s
            """,
            (source_id,),
        )

        source = _rows(cur)

        if not source:
            raise ValueError("Measurement source not found")

        cur.execute(
            """
            SELECT
                metric_name,
                raw_value,
                numeric_value,
                unit,
                value_kind,
                source_location,
                observed_on,
                period_start,
                period_end
            FROM energy_observations
            WHERE source_id = %s
            ORDER BY observation_id
            """,
            (source_id,),
        )

        return {
            "source": source[0],
            "observations": _rows(cur),
        }


def get_measurement_elevators():
    with get_connection() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT elevator_id, elevator_ref, building_name
            FROM elevators
            ORDER BY elevator_ref
            """
        )

        return _rows(cur)


def _matching_elevator(cur, elevator_id, reported_ref):
    cur.execute(
        "SELECT elevator_ref FROM elevators WHERE elevator_id = %s",
        (elevator_id,),
    )

    match = cur.fetchone()

    if (
        not match
        or not reported_ref
        or match[0].strip().upper() != reported_ref.strip().upper()
    ):
        raise ValueError(
            "Selected elevator does not match the source reference"
        )


def review_measurement_source(source_id, decision, note="", elevator_id=None):
    if decision not in {"approved", "rejected"}:
        raise ValueError("Invalid review decision")

    note = note.strip()

    if decision == "rejected" and not note:
        raise ValueError("Explain why the measurement source was rejected")

    if decision != "approved" and elevator_id is not None:
        raise ValueError("Only approved sources can be linked to an elevator")

    with get_connection() as conn, conn.cursor() as cur:
        # Lock the source to prevent conflicting review actions
        cur.execute(
            """
            SELECT reported_elevator_ref, review_status
            FROM measurement_sources
            WHERE source_id = %s
            FOR UPDATE
            """,
            (source_id,),
        )

        row = cur.fetchone()

        if not row:
            raise ValueError("Measurement source not found")

        if row[1] != "pending":
            raise ValueError("Only pending measurement sources can be reviewed")

        if elevator_id is not None:
            _matching_elevator(cur, elevator_id, row[0])

        cur.execute(
            """
            UPDATE measurement_sources
            SET review_status = %s,
                review_note = %s,
                verified_elevator_id = %s,
                reviewed_at = NOW()
            WHERE source_id = %s
            """,
            (
                decision,
                note or None,
                elevator_id,
                source_id,
            ),
        )

    return decision


def link_approved_measurement(source_id, elevator_id):
    # Allow linking after the correct elevator has been registered
    with get_connection() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT
                reported_elevator_ref,
                review_status,
                verified_elevator_id
            FROM measurement_sources
            WHERE source_id = %s
            FOR UPDATE
            """,
            (source_id,),
        )

        row = cur.fetchone()

        if not row or row[1] != "approved" or row[2] is not None:
            raise ValueError(
                "Source must be approved and not already linked"
            )

        _matching_elevator(cur, elevator_id, row[0])

        cur.execute(
            """
            UPDATE measurement_sources
            SET verified_elevator_id = %s
            WHERE source_id = %s
            """,
            (elevator_id, source_id),
        )

    return elevator_id
