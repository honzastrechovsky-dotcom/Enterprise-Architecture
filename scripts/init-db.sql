-- ============================================================
-- PostgreSQL init script: enable pgvector extension
-- This runs automatically when the container is first created.
-- The pgvector/pgvector:pg16 image ships the extension; we just
-- need to activate it in the target database.
-- ============================================================

-- Enable the vector extension (required for RAG embeddings)
CREATE EXTENSION IF NOT EXISTS vector;

-- Enable uuid-ossp for UUID generation helpers (optional but useful)
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

-- Enable pg_trgm for trigram-based text search (optional)
CREATE EXTENSION IF NOT EXISTS pg_trgm;
