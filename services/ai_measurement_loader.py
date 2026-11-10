
"""Load reviewed measurement pairs for the AI dataset."""

from database import get_connection
from services.ai_dataset import build_ai_dataset


def load_verified_measurement_pairs():
    # Read approved pairs and their original measurement periods.
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    v.pair_id,
                    v.document_id,
                    v.case_id,
                    v.elevator_id,
                    v.review_status,
                    v.before_basis,
                    v.after_basis,
                    v.before_unit,
                    v.after_unit,
                    v.conditions_verified,
                    v.specifications_verified,
                    v.before_source_id,
                    v.after_source_id,
                    v.before_kwh,
                    v.after_kwh,
                    v.before_days,
                    v.after_days,
                    b.period_start AS before_start,
                    b.period_end AS before_end,
                    a.period_start AS after_start,
                    a.period_end AS after_end
                FROM ai_verified_measurement_pairs_view v
                JOIN verified_measurement_pairs p
                    ON p.pair_id = v.pair_id
                JOIN energy_observations b
                    ON b.observation_id = p.before_observation_id
                JOIN energy_observations a
                    ON a.observation_id = p.after_observation_id
                ORDER BY v.case_id
                """
            )

            columns = [column.name for column in cur.description]
            pairs = [
                dict(zip(columns, values))
                for values in cur.fetchall()
            ]

    # Require inclusive period dates to match the reviewed duration.
    for pair in pairs:
        for period in ("before", "after"):
            start = pair.pop(f"{period}_start")
            end = pair.pop(f"{period}_end")
            days = pair[f"{period}_days"]

            if (
                start is None
                or end is None
                or (end - start).days + 1 != days
            ):
                raise ValueError(
                    f"Pair {pair['pair_id']}: {period}_days does not "
                    "match the observation date range."
                )

    return pairs


def prepare_ai_dataset():
    # Let the existing dataset module handle EnerCal matching and exclusions.
    pairs = load_verified_measurement_pairs()
    return build_ai_dataset(pairs)


def main():
    dataset, excluded = prepare_ai_dataset()
    elevators = {row["elevator_id"] for row in dataset}

    print(f"Verified AI dataset projects: {len(dataset)}")
    print(f"Distinct elevators: {len(elevators)}")
    print(f"Excluded pairs: {len(excluded)}")

    if len(elevators) < 10:
        print(
            "AI training not ready: "
            "fewer than 10 distinct verified elevators."
        )
    else:
        print(
            "Dataset meets the minimum count; "
            "review it before training."
        )


if __name__ == "__main__":
    main()
