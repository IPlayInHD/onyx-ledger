-- =============================================================================
-- Onyx Ledger — 37 · Schema comment convergence  (schema: ioe)
-- Alembic revision: 0043_schema_comment_convergence
--
-- Entry 7 (production engineering quality gate), schema-drift governance.
--
-- The Alembic comparison between the applied schema and the SQLAlchemy metadata
-- must be readable, because a comparison full of known noise cannot report an
-- unknown change. Almost all of the divergence was resolved by making the model
-- truthful — the models now carry the comments and the NOT NULL that the applied
-- schema already had, and nothing about the database changed.
--
-- One divergence points the other way: `ioe.strategy_portfolio.portfolio_result_hash`
-- carries a comment in the model that the database never received. Deleting the
-- documentation to silence the comparison would be the wrong direction, so the
-- database gets the comment instead.
--
-- COMMENT-ONLY. No column, constraint, index, trigger, policy, grant, or row is
-- touched. `COMMENT ON` takes a brief ACCESS EXCLUSIVE lock on the catalog entry
-- and rewrites nothing, so this is safe to apply to a live database.
-- =============================================================================

COMMENT ON COLUMN ioe.strategy_portfolio.portfolio_result_hash IS
  'Domain-separated hash of the portfolio''s canonical form. The expected value a portfolio integrity check replays against; NULL on portfolios sealed before the column was written, which makes them unverifiable rather than mismatched.';
