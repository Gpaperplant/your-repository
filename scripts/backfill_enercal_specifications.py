
import argparse

from database import get_connection
from services.enercal_specs import extract_specs


def backfill(document_id):
    # Load the original PDF without changing its reviewed revisions.
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT pdf_bytes FROM source_documents WHERE document_id = %s",
                (document_id,),
            )
            row = cur.fetchone()

            if row is None:
                raise ValueError(f"Document {document_id} was not found")

            records = extract_specs(bytes(row[0]))

            if not records:
                raise ValueError("No additional fields were extracted")

            # Re-running updates only the additional extraction records.
            cur.executemany(
                """
                INSERT INTO enercal_specifications
                    (document_id, field_name, field_value, unit, source_page)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (document_id, field_name)
                DO UPDATE SET
                    field_value = EXCLUDED.field_value,
                    unit = EXCLUDED.unit,
                    source_page = EXCLUDED.source_page
                """,
                [(document_id, *record) for record in records],
            )

    print(f"Document {document_id}: saved {len(records)} fields and checks")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("document_id", type=int)
    args = parser.parse_args()
    backfill(args.document_id)
