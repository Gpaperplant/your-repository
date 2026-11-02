
-- =====================================================
-- KONE FYP DATABASE
-- 001: Elevator and Modernisation Case Registration
-- =====================================================


-- TABLE 1: Elevators
-- Stores information about individual elevators.

CREATE TABLE IF NOT EXISTS elevators (

    elevator_id BIGINT
        GENERATED ALWAYS AS IDENTITY
        PRIMARY KEY,

    elevator_ref VARCHAR(100)
        NOT NULL UNIQUE,

    building_name TEXT,

    location_description TEXT,

    created_at TIMESTAMPTZ
        NOT NULL DEFAULT NOW()

);


-- TABLE 2: Modernisation Cases
-- Each modernisation case belongs to an elevator.

CREATE TABLE IF NOT EXISTS modernisation_cases (

    case_id BIGINT
        GENERATED ALWAYS AS IDENTITY
        PRIMARY KEY,

    case_ref VARCHAR(100)
        NOT NULL UNIQUE,

    elevator_id BIGINT NOT NULL
        REFERENCES elevators(elevator_id),

    status VARCHAR(30)
        NOT NULL DEFAULT 'registered'
        CHECK (
            status IN (
                'registered',
                'under_review',
                'approved',
                'completed'
            )
        ),

    created_at TIMESTAMPTZ
        NOT NULL DEFAULT NOW()

);

