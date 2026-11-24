import hashlib
import re
from io import BytesIO
from zipfile import is_zipfile

from database import get_connection


MAX_FILE_BYTES = 15 * 1024 * 1024
EXTENSIONS = {
    "mod_pdf": ".pdf",
    "iso_excel": ".xlsx",
    "iso_test_pdf": ".pdf",
    "meter_csv": ".csv",
}
SOURCE_CLASSES = {"real", "synthetic", "test_fixture", "unknown"}


def _normalise_ref(value):
    return re.sub(r"[^A-Z0-9]", "", str(value or "").upper())


def _validate_upload(document_type, filename, file_bytes, reported_elevator_ref,
                     source_classification):
    if document_type not in EXTENSIONS:
        raise ValueError("Unsupported document type.")
    if source_classification not in SOURCE_CLASSES:
        raise ValueError("Invalid source classification.")
    if not isinstance(filename, str) or not filename.strip():
        raise ValueError("A filename is required.")
    filename = filename.strip()
    if any(char in filename for char in ("/", "\\", "\x00")):
        raise ValueError("Use a filename without a directory path.")
    if not filename.lower().endswith(EXTENSIONS[document_type]):
        raise ValueError("Filename does not match the document type.")
    if not isinstance(file_bytes, bytes) or not 0 < len(file_bytes) <= MAX_FILE_BYTES:
        raise ValueError("File must be nonempty and no larger than 15 MB.")
    if document_type.endswith("pdf") and not file_bytes.startswith(b"%PDF-"):
        raise ValueError("Invalid PDF file.")
    if document_type == "iso_excel" and not is_zipfile(BytesIO(file_bytes)):
        raise ValueError("Invalid Excel workbook.")
    if document_type == "meter_csv":
        try:
            file_bytes.decode("utf-8-sig")
        except UnicodeDecodeError as error:
            raise ValueError("Meter CSV must use UTF-8 encoding.") from error
    if reported_elevator_ref is not None and not isinstance(reported_elevator_ref, str):
        raise ValueError("Elevator reference must be text.")
    reported_elevator_ref = (reported_elevator_ref or "").strip() or None
    if document_type == "iso_excel" and not _normalise_ref(reported_elevator_ref):
        raise ValueError("ISO Excel requires an elevator reference.")
    return filename, reported_elevator_ref


def insert_project_document(cursor, *, case_ref, document_type, filename,
                            file_bytes, reported_elevator_ref=None,
                            source_classification="unknown"):
    filename, reported_elevator_ref = _validate_upload(
        document_type, filename, file_bytes, reported_elevator_ref,
        source_classification,
    )
    if not isinstance(case_ref, str) or not case_ref.strip():
        raise ValueError("A modernisation case reference is required.")

    cursor.execute(
        """
        SELECT m.case_id, e.elevator_ref
        FROM modernisation_cases m
        JOIN elevators e ON e.elevator_id = m.elevator_id
        WHERE m.case_ref = %s
        FOR UPDATE OF m
        """,
        (case_ref.strip(),),
    )
    case = cursor.fetchone()
    if case is None:
        raise ValueError("Modernisation case not found.")

    case_id, registered_ref = case
    if document_type == "mod_pdf" and registered_ref.startswith("TEST-LIFT-"):
        source_classification = "test_fixture"

    test_fixture = (
        source_classification == "test_fixture"
        and registered_ref.startswith("TEST-LIFT-")
    )
    if (document_type == "iso_excel" and not test_fixture
            and _normalise_ref(reported_elevator_ref) != _normalise_ref(registered_ref)):
        raise ValueError("ISO elevator reference does not match this case.")

    checksum = hashlib.sha256(file_bytes).hexdigest()
    cursor.execute(
        """
        INSERT INTO project_documents (
            case_id, document_type, source_classification,
            reported_elevator_ref, filename, file_sha256, file_content
        ) VALUES (%s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (case_id, document_type, file_sha256) DO NOTHING
        RETURNING document_id
        """,
        (
            case_id, document_type, source_classification,
            reported_elevator_ref, filename, checksum, file_bytes,
        ),
    )
    inserted = cursor.fetchone()
    if inserted is None:
        cursor.execute(
            """
            SELECT document_id FROM project_documents
            WHERE case_id = %s AND document_type = %s AND file_sha256 = %s
            """,
            (case_id, document_type, checksum),
        )
        existing = cursor.fetchone()
        if existing is None:
            raise RuntimeError("Could not retrieve the existing document.")
        document_id = existing[0]
    else:
        document_id = inserted[0]

    return {
        "document_id": document_id,
        "case_id": case_id,
        "document_type": document_type,
        "created": inserted is not None,
    }


def save_project_document(*, case_ref, document_type, filename, file_bytes,
                          reported_elevator_ref=None, source_classification="unknown"):
    with get_connection() as connection, connection.cursor() as cursor:
        return insert_project_document(
            cursor,
            case_ref=case_ref,
            document_type=document_type,
            filename=filename,
            file_bytes=file_bytes,
            reported_elevator_ref=reported_elevator_ref,
            source_classification=source_classification,
        )


def get_project_document(document_id):
    with get_connection() as connection, connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT document_id, case_id, document_type, filename,
                   file_sha256, file_content, reported_elevator_ref,
                   source_classification, uploaded_at
            FROM project_documents
            WHERE document_id = %s
            """,
            (document_id,),
        )
        row = cursor.fetchone()
    if row is None:
        raise ValueError("Document not found.")
    columns = (
        "document_id", "case_id", "document_type", "filename", "file_sha256",
        "file_content", "reported_elevator_ref", "source_classification",
        "uploaded_at",
    )
    return dict(zip(columns, row))
