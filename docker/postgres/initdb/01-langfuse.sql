-- Langfuse v2 uses its own database in the same postgres (decisions 2026-11-04 [root]).
-- Runs only on first initialisation of the postgres volume.
CREATE DATABASE langfuse OWNER domino;
