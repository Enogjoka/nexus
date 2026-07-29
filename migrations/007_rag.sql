-- NEXUS Task 11: RAG memory over state vectors.
--
-- `embedding` holds the deterministic, locally-computed feature vector for a
-- state_vectors row (see fusion/rag.py -- no external embedding API, no
-- pgvector; JSONB + numpy cosine is sufficient at this corpus size).
--
-- `signals.state_vector_id` back-links an issued signal to the world-state it
-- was born into, which is what makes outcome-weighted recall possible.
--
-- Idempotency comes from schema_migrations (this file runs exactly once,
-- recorded by filename) -- no IF NOT EXISTS gymnastics needed.

ALTER TABLE state_vectors ADD COLUMN embedding JSONB;

ALTER TABLE signals ADD COLUMN state_vector_id BIGINT REFERENCES state_vectors(id);

CREATE INDEX IF NOT EXISTS idx_signals_state_vector_id ON signals (state_vector_id);
