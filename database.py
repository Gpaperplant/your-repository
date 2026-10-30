
import os
from pathlib import Path

import psycopg
from dotenv import load_dotenv

# Load environment variables from the project's .env file.
load_dotenv(Path(__file__).resolve().parent / ".env")


def get_connection():
    """Connect to the PostgreSQL database."""
    password = os.getenv("POSTGRES_PASSWORD")

    if not password:
        raise RuntimeError(
            "POSTGRES_PASSWORD is missing. Check your .env file."
        )

    return psycopg.connect(
        host="127.0.0.1",
        port=5432,
        dbname="request_app",
        user="app_user",
        password=password,
        connect_timeout=5,
    )


def create_request(requester_name, title, description):
    """Save a new request and return its ID."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO requests
                    (requester_name, title, description)
                VALUES (%s, %s, %s)
                RETURNING id
                """,
                (requester_name, title, description),
            )
            return cur.fetchone()[0]


def get_requests(status=None):
    """Retrieve all requests, optionally filtered by status."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            if status is None:
                cur.execute(
                    """
                    SELECT id, requester_name, title,
                           description, status, created_at, updated_at
                    FROM requests
                    ORDER BY created_at DESC
                    """
                )
            else:
                cur.execute(
                    """
                    SELECT id, requester_name, title,
                           description, status, created_at, updated_at
                    FROM requests
                    WHERE status = %s
                    ORDER BY created_at DESC
                    """,
                    (status,),
                )

            return cur.fetchall()


def add_response(request_id, responder_name, message):
    """Save a response to an existing request."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO responses
                    (request_id, responder_name, message)
                VALUES (%s, %s, %s)
                RETURNING id
                """,
                (request_id, responder_name, message),
            )
            return cur.fetchone()[0]


def update_request_status(request_id, status):
    """Update a request's status and modification time."""
    allowed_statuses = {
        "Pending", "In Progress", "Completed", "Rejected"
    }

    if status not in allowed_statuses:
        raise ValueError("Invalid request status.")

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE requests
                SET status = %s, updated_at = CURRENT_TIMESTAMP
                WHERE id = %s
                RETURNING id
                """,
                (status, request_id),
            )

            if cur.fetchone() is None:
                raise ValueError("Request not found.")


def get_responses(request_id):
    """Retrieve all responses associated with a request."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, responder_name, message, created_at
                FROM responses
                WHERE request_id = %s
                ORDER BY created_at ASC
                """,
                (request_id,),
            )
            return cur.fetchall()
