-- Local development bootstrap for the platform PostgreSQL instance.
-- 1. pgvector for agent long-term memory and RAG embeddings.
-- 2. A dedicated database for the self-hosted Langfuse deployment.
CREATE EXTENSION IF NOT EXISTS vector;
CREATE DATABASE langfuse;
