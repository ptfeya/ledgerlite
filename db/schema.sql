-- Idempotent: safe to apply repeatedly (app.seed and the test suite both do).
-- Amounts are integer cents. Debits are negative, credits positive, so the
-- whole ledger sums to zero.

CREATE TABLE IF NOT EXISTS users (
    id            bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    email         text NOT NULL UNIQUE CHECK (email = lower(email)),
    password_hash text NOT NULL,
    is_admin      boolean NOT NULL DEFAULT false,
    created_at    timestamptz NOT NULL DEFAULT now()
);

-- Upgrade existing databases without granting privileges to existing users.
ALTER TABLE users ADD COLUMN IF NOT EXISTS is_admin boolean NOT NULL DEFAULT false;

-- One bounded counter per user, shared by all UI sessions and app workers.
CREATE TABLE IF NOT EXISTS transfer_rate_limits (
    user_id           bigint PRIMARY KEY REFERENCES users (id) ON DELETE CASCADE,
    window_started_at timestamptz NOT NULL DEFAULT statement_timestamp(),
    attempts          integer NOT NULL DEFAULT 1 CHECK (attempts > 0)
);

-- System accounts (e.g. the funding account) have no owning user, so no login
-- can ever reach them. User accounts always have an owner.
CREATE TABLE IF NOT EXISTS accounts (
    id         bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    user_id    bigint REFERENCES users (id),
    name       text NOT NULL,
    currency   char(3) NOT NULL DEFAULT 'USD',
    is_system  boolean NOT NULL DEFAULT false,
    created_at timestamptz NOT NULL DEFAULT now(),
    CHECK ((is_system AND user_id IS NULL) OR (NOT is_system AND user_id IS NOT NULL))
);
CREATE INDEX IF NOT EXISTS accounts_user_id_idx ON accounts (user_id);

-- user_id is NULL only for system-initiated transfers (seed funding).
-- The unique index scopes idempotency keys per user.
CREATE TABLE IF NOT EXISTS transfers (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id         bigint REFERENCES users (id),
    idempotency_key text NOT NULL,
    request_hash    text NOT NULL,
    from_account_id bigint NOT NULL REFERENCES accounts (id),
    to_account_id   bigint NOT NULL REFERENCES accounts (id),
    amount_cents    bigint NOT NULL CHECK (amount_cents > 0),
    created_at      timestamptz NOT NULL DEFAULT now(),
    CHECK (from_account_id <> to_account_id)
);
CREATE UNIQUE INDEX IF NOT EXISTS transfers_user_key_uq ON transfers (user_id, idempotency_key);

CREATE TABLE IF NOT EXISTS ledger_entries (
    id           bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    transfer_id  uuid NOT NULL REFERENCES transfers (id),
    account_id   bigint NOT NULL REFERENCES accounts (id),
    amount_cents bigint NOT NULL CHECK (amount_cents <> 0),
    created_at   timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ledger_entries_account_idx ON ledger_entries (account_id, id);
CREATE INDEX IF NOT EXISTS ledger_entries_transfer_idx ON ledger_entries (transfer_id);

-- Defense in depth: a transaction cannot commit entries for a transfer that
-- do not sum to zero.
CREATE OR REPLACE FUNCTION check_transfer_balanced() RETURNS trigger AS $$
BEGIN
    IF (SELECT COALESCE(SUM(amount_cents), 0) FROM ledger_entries
        WHERE transfer_id = NEW.transfer_id) <> 0 THEN
        RAISE EXCEPTION 'ledger entries for transfer % do not sum to zero', NEW.transfer_id
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NULL;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS ledger_entries_balanced ON ledger_entries;
CREATE CONSTRAINT TRIGGER ledger_entries_balanced
    AFTER INSERT ON ledger_entries
    DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW EXECUTE FUNCTION check_transfer_balanced();

-- The ledger is append-only.
CREATE OR REPLACE FUNCTION forbid_ledger_mutation() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'ledger_entries is append-only';
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS ledger_entries_append_only ON ledger_entries;
CREATE TRIGGER ledger_entries_append_only
    BEFORE UPDATE OR DELETE ON ledger_entries
    FOR EACH ROW EXECUTE FUNCTION forbid_ledger_mutation();

-- Transactional outbox for outbound webhooks. An event row is written in the
-- same transaction as the transfer it describes, so it exists exactly when the
-- money moved. The body is the exact JSON sent on every attempt and replay.
-- Delivered rows are kept; failed events move to webhook_dead_letter.
CREATE TABLE IF NOT EXISTS webhook_outbox (
    event_id         uuid PRIMARY KEY,
    event_type       text NOT NULL CHECK (event_type IN ('transfer.completed')),
    transfer_id      uuid NOT NULL REFERENCES transfers (id),
    body             text NOT NULL CHECK (body IS JSON OBJECT),
    status           text NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'delivered')),
    -- Recorded delivery outcomes in the current cycle (six at most).
    attempts         integer NOT NULL DEFAULT 0 CHECK (attempts BETWEEN 0 AND 6),
    next_attempt_at  timestamptz NOT NULL DEFAULT now(),
    -- Only a numeric HTTP status or a short sanitized classification, never raw
    -- responses, URLs or secrets.
    last_status_code integer CHECK (last_status_code BETWEEN 100 AND 599),
    last_error       text CHECK (char_length(last_error) <= 100),
    created_at       timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS webhook_outbox_transfer_event_uq
    ON webhook_outbox (transfer_id, event_type);
CREATE INDEX IF NOT EXISTS webhook_outbox_due_idx
    ON webhook_outbox (next_attempt_at, event_id) WHERE status = 'pending';

-- Events that exhausted their attempts or got a terminal response. A row lives
-- in exactly one of the two tables; moves between them are single transactions.
CREATE TABLE IF NOT EXISTS webhook_dead_letter (
    event_id         uuid PRIMARY KEY,
    event_type       text NOT NULL CHECK (event_type IN ('transfer.completed')),
    transfer_id      uuid NOT NULL REFERENCES transfers (id),
    body             text NOT NULL CHECK (body IS JSON OBJECT),
    attempts         integer NOT NULL CHECK (attempts BETWEEN 1 AND 6),
    last_status_code integer CHECK (last_status_code BETWEEN 100 AND 599),
    last_error       text CHECK (char_length(last_error) <= 100),
    created_at       timestamptz NOT NULL,
    failed_at        timestamptz NOT NULL DEFAULT now(),
    CHECK (last_status_code IS NOT NULL OR last_error IS NOT NULL)
);
CREATE UNIQUE INDEX IF NOT EXISTS webhook_dead_letter_transfer_event_uq
    ON webhook_dead_letter (transfer_id, event_type);
CREATE INDEX IF NOT EXISTS webhook_dead_letter_failed_idx
    ON webhook_dead_letter (failed_at, event_id);
