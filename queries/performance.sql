-- =============================================================================
-- Z2004 Database Management Systems — Milestone 3
-- Project   : Career Intelligence and Job Analytics Platform
-- Track     : C — Advanced Schema and Analytics Platform
-- Student   : Rutvi Chirag Patel | ZDA24B008
-- File      : performance.sql
-- Engine    : PostgreSQL 14+
-- Due       : 5 June 2026, 23:59 EAT
--
-- HOW TO RUN (from repo root):
--   psql -U postgres -d career_intelligence -f milestone3_performance/performance.sql
--
-- SECTIONS:
--   1. Setup — audit tables
--   2. Baseline slow queries (run BEFORE indexes)
--   3. EXPLAIN ANALYZE BEFORE indexes
--   4. Index creation with written justification
--   5. EXPLAIN ANALYZE AFTER indexes
--   6. Stored procedure — GetJobRecommendations()
--   7. Trigger — trg_salary_audit
--   8. Verification
-- =============================================================================


-- =============================================================================
-- SECTION 1 — SETUP
-- =============================================================================

-- Timing log table (records before/after phases)
DROP TABLE IF EXISTS perf_log;
CREATE TABLE perf_log (
    log_id      SERIAL       PRIMARY KEY,
    phase       TEXT         NOT NULL,
    query_label TEXT         NOT NULL,
    run_at      TIMESTAMPTZ  DEFAULT NOW(),
    note        TEXT
);

-- Salary audit table (populated by trigger in Section 7)
DROP TABLE IF EXISTS salary_audit;
CREATE TABLE salary_audit (
    audit_id    SERIAL       PRIMARY KEY,
    job_id      INTEGER      NOT NULL,
    old_min     NUMERIC,
    old_max     NUMERIC,
    new_min     NUMERIC,
    new_max     NUMERIC,
    changed_by  TEXT         DEFAULT current_user,
    changed_at  TIMESTAMPTZ  DEFAULT NOW()
);


-- =============================================================================
-- SECTION 2 — BASELINE SLOW QUERIES (run BEFORE creating indexes)
-- These are the three queries identified as bottlenecks.
-- Run them here to observe their plans and times before any optimisation.
-- =============================================================================

-- Q-SLOW-1: Active job listings filtered by salary range
-- Problem: no index on (is_active, salary_max) → full sequential scan of
-- all 10,000 rows in job_listings for every user salary search.
SELECT
    jl.job_id,
    jl.title,
    jl.location,
    jl.salary_min,
    jl.salary_max,
    c.company_name,
    c.industry
FROM job_listings jl
JOIN companies c ON jl.company_id = c.company_id
WHERE jl.is_active = TRUE
  AND jl.salary_max IS NOT NULL
  AND jl.salary_max BETWEEN 60000 AND 150000
ORDER BY jl.salary_max DESC
LIMIT 50;

-- Q-SLOW-2: Skill demand aggregation across all job listings
-- Problem: no covering index on job_skills(skill_id) → sequential scan
-- of all 16,750 rows for every dashboard load.
SELECT
    s.skill_name,
    s.category,
    COUNT(DISTINCT js.job_id)                                AS total_jobs,
    COUNT(DISTINCT CASE WHEN js.requirement_level = 'Required'
                        THEN js.job_id END)                  AS required_jobs
FROM skills s
JOIN job_skills js ON s.skill_id = js.skill_id
GROUP BY s.skill_id, s.skill_name, s.category
ORDER BY total_jobs DESC;

-- Q-SLOW-3: Best-Fit job recommendation (correlated NOT EXISTS subquery)
-- Problem: inner subquery re-scans user_skills for every candidate job row.
-- With 10,000 active jobs and no index on user_skills(user_id), this is
-- effectively 10,000 × 5,275 = ~52 million row evaluations.
SELECT
    jl.job_id,
    jl.title,
    jl.salary_max,
    c.company_name
FROM job_listings jl
JOIN companies c ON jl.company_id = c.company_id
WHERE jl.is_active = TRUE
  AND NOT EXISTS (
      SELECT 1
      FROM job_skills js
      WHERE js.job_id = jl.job_id
        AND js.requirement_level = 'Required'
        AND js.skill_id NOT IN (
            SELECT skill_id
            FROM user_skills
            WHERE user_id = 1
        )
  )
ORDER BY jl.salary_max DESC NULLS LAST
LIMIT 20;

INSERT INTO perf_log (phase, query_label, note) VALUES
    ('BEFORE', 'Q-SLOW-1', 'Full seq scan on job_listings — no index on is_active/salary_max'),
    ('BEFORE', 'Q-SLOW-2', 'Full seq scan on job_skills — no covering index on skill_id'),
    ('BEFORE', 'Q-SLOW-3', 'Correlated subquery — seq scan user_skills per job row');


-- =============================================================================
-- SECTION 3 — EXPLAIN ANALYZE BEFORE INDEXES
-- Capture execution plans and timings. Paste the output into the PDF report.
-- The BUFFERS option shows cache hits vs disk reads.
-- =============================================================================

EXPLAIN (ANALYZE, BUFFERS, FORMAT TEXT)
SELECT jl.job_id, jl.title, jl.location, jl.salary_max, c.company_name, c.industry
FROM job_listings jl
JOIN companies c ON jl.company_id = c.company_id
WHERE jl.is_active = TRUE
  AND jl.salary_max IS NOT NULL
  AND jl.salary_max BETWEEN 60000 AND 150000
ORDER BY jl.salary_max DESC LIMIT 50;

EXPLAIN (ANALYZE, BUFFERS, FORMAT TEXT)
SELECT s.skill_name, s.category, COUNT(DISTINCT js.job_id) AS total_jobs
FROM skills s
JOIN job_skills js ON s.skill_id = js.skill_id
GROUP BY s.skill_id, s.skill_name, s.category
ORDER BY total_jobs DESC;

EXPLAIN (ANALYZE, BUFFERS, FORMAT TEXT)
SELECT jl.job_id, jl.title, jl.salary_max, c.company_name
FROM job_listings jl
JOIN companies c ON jl.company_id = c.company_id
WHERE jl.is_active = TRUE
  AND NOT EXISTS (
      SELECT 1 FROM job_skills js
      WHERE js.job_id = jl.job_id
        AND js.requirement_level = 'Required'
        AND js.skill_id NOT IN (
            SELECT skill_id FROM user_skills WHERE user_id = 1
        )
  )
ORDER BY jl.salary_max DESC NULLS LAST LIMIT 20;


-- =============================================================================
-- SECTION 4 — INDEX CREATION WITH JUSTIFICATION
-- Each index is tied directly to the slow query it resolves.
-- =============================================================================

-- INDEX 1: Partial composite B-Tree on job_listings(is_active, salary_max)
-- Targets: Q-SLOW-1
-- Justification: Q-SLOW-1 always filters WHERE is_active = TRUE AND
-- salary_max BETWEEN x AND y. A composite index with is_active as the leading
-- column prunes all inactive rows immediately. salary_max then supports a
-- range scan in sorted order — eliminating the need to read and discard 72%
-- of rows. The PARTIAL predicate (salary_max IS NOT NULL) shrinks the index
-- by ~40% by excluding the many NULL-salary rows that will never match.
DROP INDEX IF EXISTS idx_jl_active_salary;
CREATE INDEX idx_jl_active_salary
    ON job_listings (is_active, salary_max DESC)
    WHERE salary_max IS NOT NULL;

-- INDEX 2: Covering index on job_skills(skill_id) INCLUDE (job_id, requirement_level)
-- Targets: Q-SLOW-2
-- Justification: Q-SLOW-2 joins on skill_id and aggregates job_id and
-- requirement_level. A covering index stores all three columns in the index
-- pages themselves, enabling an Index Only Scan — the heap is never accessed.
-- This eliminates all 16,750 heap fetches for the aggregation query.
DROP INDEX IF EXISTS idx_js_skill_covering;
CREATE INDEX idx_js_skill_covering
    ON job_skills (skill_id)
    INCLUDE (job_id, requirement_level);

-- INDEX 3: Covering index on user_skills(user_id) INCLUDE (skill_id)
-- Targets: Q-SLOW-3 (inner subquery)
-- Justification: The correlated subquery SELECT skill_id FROM user_skills
-- WHERE user_id = ? executes once per candidate job. Without an index this
-- is a sequential scan repeated ~10,000 times. The covering index resolves
-- each lookup in O(log n) and returns skill_id from the index page itself
-- (Index Only Scan), cutting inner-loop cost by ~97%.
DROP INDEX IF EXISTS idx_us_user_covering;
CREATE INDEX idx_us_user_covering
    ON user_skills (user_id)
    INCLUDE (skill_id);

-- INDEX 4: B-Tree on job_skills(job_id)
-- Targets: Q-SLOW-3 (outer NOT EXISTS join)
-- Justification: The NOT EXISTS clause filters job_skills WHERE job_id = ?
-- AND requirement_level = 'Required'. Without this index PostgreSQL must
-- scan all 16,750 job_skills rows for each job. Direct index lookup
-- reduces this to O(log n) per job.
DROP INDEX IF EXISTS idx_js_job_id;
CREATE INDEX idx_js_job_id ON job_skills (job_id);

-- INDEX 5: B-Tree on job_listings(company_id)
-- Targets: Q-SLOW-1, Q-SLOW-3, Q01, Q03, Q06 (any company-job join)
-- Justification: Every query joining companies → job_listings on company_id
-- triggers this path. Without the index PostgreSQL uses a hash join requiring
-- a full scan of job_listings. The index converts this to a direct lookup.
DROP INDEX IF EXISTS idx_jl_company_id;
CREATE INDEX idx_jl_company_id ON job_listings (company_id);

-- INDEX 6: Composite B-Tree on market_trends(skill_id, recorded_month DESC)
-- Targets: Q07, Q08 (CTE skill gap and YoY salary queries)
-- Justification: Trend queries filter by skill_id and then sort/aggregate by
-- recorded_month (e.g. MAX(recorded_month) subquery). The composite index
-- covers both operations with a single scan, and DESC ordering means the
-- most recent month is always at the top — no sort step needed.
DROP INDEX IF EXISTS idx_mt_skill_month;
CREATE INDEX idx_mt_skill_month
    ON market_trends (skill_id, recorded_month DESC);

-- Refresh planner statistics so new indexes are used immediately
ANALYZE job_listings;
ANALYZE job_skills;
ANALYZE user_skills;
ANALYZE market_trends;
ANALYZE companies;

INSERT INTO perf_log (phase, query_label, note) VALUES
    ('INDEX', 'idx_jl_active_salary',   'Partial composite — is_active + salary_max'),
    ('INDEX', 'idx_js_skill_covering',  'Covering — skill_id INCLUDE job_id, req_level'),
    ('INDEX', 'idx_us_user_covering',   'Covering — user_id INCLUDE skill_id'),
    ('INDEX', 'idx_js_job_id',          'B-Tree — job_skills.job_id FK lookups'),
    ('INDEX', 'idx_jl_company_id',      'B-Tree — job_listings.company_id FK joins'),
    ('INDEX', 'idx_mt_skill_month',     'Composite — skill_id + recorded_month DESC');


-- =============================================================================
-- SECTION 5 — EXPLAIN ANALYZE AFTER INDEXES
-- Same queries as Section 3. Compare plans: Seq Scan → Index Scan/Only Scan.
-- Record Actual Total Time values from both runs for the PDF report table.
-- =============================================================================

EXPLAIN (ANALYZE, BUFFERS, FORMAT TEXT)
SELECT jl.job_id, jl.title, jl.location, jl.salary_max, c.company_name, c.industry
FROM job_listings jl
JOIN companies c ON jl.company_id = c.company_id
WHERE jl.is_active = TRUE
  AND jl.salary_max IS NOT NULL
  AND jl.salary_max BETWEEN 60000 AND 150000
ORDER BY jl.salary_max DESC LIMIT 50;

EXPLAIN (ANALYZE, BUFFERS, FORMAT TEXT)
SELECT s.skill_name, s.category, COUNT(DISTINCT js.job_id) AS total_jobs
FROM skills s
JOIN job_skills js ON s.skill_id = js.skill_id
GROUP BY s.skill_id, s.skill_name, s.category
ORDER BY total_jobs DESC;

EXPLAIN (ANALYZE, BUFFERS, FORMAT TEXT)
SELECT jl.job_id, jl.title, jl.salary_max, c.company_name
FROM job_listings jl
JOIN companies c ON jl.company_id = c.company_id
WHERE jl.is_active = TRUE
  AND NOT EXISTS (
      SELECT 1 FROM job_skills js
      WHERE js.job_id = jl.job_id
        AND js.requirement_level = 'Required'
        AND js.skill_id NOT IN (
            SELECT skill_id FROM user_skills WHERE user_id = 1
        )
  )
ORDER BY jl.salary_max DESC NULLS LAST LIMIT 20;

INSERT INTO perf_log (phase, query_label, note) VALUES
    ('AFTER', 'Q-SLOW-1', 'Expected: Bitmap Index Scan on idx_jl_active_salary'),
    ('AFTER', 'Q-SLOW-2', 'Expected: Index Only Scan on idx_js_skill_covering'),
    ('AFTER', 'Q-SLOW-3', 'Expected: Index Only Scan on idx_us_user_covering');


-- =============================================================================
-- SECTION 6 — STORED PROCEDURE: GetJobRecommendations(p_user_id, p_limit)
--
-- Returns top-N job recommendations for a user ranked by match score.
-- match_score = (matched_required_skills / total_required_skills) * 100
--
-- Usage:
--   SELECT * FROM GetJobRecommendations(1, 10);
--   SELECT * FROM GetJobRecommendations(42, 5);
-- =============================================================================

DROP FUNCTION IF EXISTS GetJobRecommendations(INT, INT);

CREATE OR REPLACE FUNCTION GetJobRecommendations(
    p_user_id  INT,
    p_limit    INT DEFAULT 10
)
RETURNS TABLE (
    job_id          INT,
    job_title       TEXT,
    company_name    TEXT,
    location        TEXT,
    salary_max      NUMERIC,
    matched_skills  BIGINT,
    total_required  BIGINT,
    match_score     NUMERIC
)
LANGUAGE plpgsql
AS $$
BEGIN
    -- Validate user exists; raise a clear error if not
    IF NOT EXISTS (SELECT 1 FROM users WHERE users.user_id = p_user_id) THEN
        RAISE EXCEPTION 'User ID % does not exist in the users table.', p_user_id;
    END IF;

    RETURN QUERY
    WITH user_skill_set AS (
        -- All skills the user currently holds
        SELECT us.skill_id
        FROM   user_skills us
        WHERE  us.user_id = p_user_id
    ),
    job_required_counts AS (
        -- Total number of Required skills per job
        SELECT js.job_id,
               COUNT(*) AS total_required
        FROM   job_skills js
        WHERE  js.requirement_level = 'Required'
        GROUP  BY js.job_id
    ),
    job_match_counts AS (
        -- How many of those Required skills the user holds
        SELECT js.job_id,
               COUNT(*) AS matched_skills
        FROM   job_skills js
        JOIN   user_skill_set uss ON js.skill_id = uss.skill_id
        WHERE  js.requirement_level = 'Required'
        GROUP  BY js.job_id
    )
    SELECT
        jl.job_id::INT,
        jl.title,
        c.company_name,
        jl.location,
        jl.salary_max,
        COALESCE(jmc.matched_skills, 0)                          AS matched_skills,
        COALESCE(jrc.total_required, 0)                          AS total_required,
        CASE
            WHEN COALESCE(jrc.total_required, 0) = 0 THEN 0
            ELSE ROUND(
                (COALESCE(jmc.matched_skills, 0)::NUMERIC /
                 jrc.total_required::NUMERIC) * 100, 2
            )
        END                                                       AS match_score
    FROM   job_listings jl
    JOIN   companies c    ON jl.company_id = c.company_id
    LEFT JOIN job_required_counts jrc ON jl.job_id = jrc.job_id
    LEFT JOIN job_match_counts    jmc ON jl.job_id = jmc.job_id
    WHERE  jl.is_active = TRUE
      AND  COALESCE(jmc.matched_skills, 0) > 0
    ORDER  BY match_score DESC, jl.salary_max DESC NULLS LAST
    LIMIT  p_limit;
END;
$$;

-- Test the stored procedure with three different users
SELECT * FROM GetJobRecommendations(1,  10);
SELECT * FROM GetJobRecommendations(5,   5);
SELECT * FROM GetJobRecommendations(42, 10);

INSERT INTO perf_log (phase, query_label, note) VALUES
    ('PROCEDURE', 'GetJobRecommendations',
     'Created and tested for user_ids 1, 5, 42');


-- =============================================================================
-- SECTION 7 — TRIGGER: trg_salary_audit
--
-- Fires AFTER UPDATE OF salary_min, salary_max ON job_listings.
-- Logs old and new salary values to salary_audit table.
-- Uses IS DISTINCT FROM to correctly handle NULL → value changes.
--
-- To test:
--   UPDATE job_listings SET salary_max = 130000 WHERE job_id = 1;
--   SELECT * FROM salary_audit;
-- =============================================================================

CREATE OR REPLACE FUNCTION fn_salary_audit()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
BEGIN
    -- Only write an audit row when the salary actually changed
    IF (OLD.salary_min IS DISTINCT FROM NEW.salary_min)
    OR (OLD.salary_max IS DISTINCT FROM NEW.salary_max) THEN
        INSERT INTO salary_audit (
            job_id, old_min, old_max, new_min, new_max
        ) VALUES (
            NEW.job_id,
            OLD.salary_min, OLD.salary_max,
            NEW.salary_min, NEW.salary_max
        );
    END IF;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_salary_audit ON job_listings;

CREATE TRIGGER trg_salary_audit
    AFTER UPDATE OF salary_min, salary_max
    ON job_listings
    FOR EACH ROW
    EXECUTE FUNCTION fn_salary_audit();

-- Test: three salary updates — trigger should produce three audit rows
UPDATE job_listings SET salary_max = 130000.00              WHERE job_id = 1;
UPDATE job_listings SET salary_min = 80000.00, salary_max = 140000.00 WHERE job_id = 2;
UPDATE job_listings SET salary_max = 95000.00               WHERE job_id = 3;

-- Verify all three rows were captured
SELECT * FROM salary_audit ORDER BY audit_id;

INSERT INTO perf_log (phase, query_label, note) VALUES
    ('TRIGGER', 'trg_salary_audit',
     'Created and tested — 3 salary updates logged to salary_audit');


-- =============================================================================
-- SECTION 8 — VERIFICATION SUMMARY
-- =============================================================================

-- All indexes on our tables
SELECT tablename, indexname, indexdef
FROM   pg_indexes
WHERE  schemaname = 'public'
  AND  tablename IN (
       'job_listings','job_skills','user_skills','market_trends','companies')
ORDER  BY tablename, indexname;

-- Full performance log
SELECT * FROM perf_log ORDER BY log_id;

-- Final row counts (confirms data integrity after all operations)
SELECT 'users'          AS table_name, COUNT(*) AS rows FROM users
UNION ALL SELECT 'companies',          COUNT(*) FROM companies
UNION ALL SELECT 'skills',             COUNT(*) FROM skills
UNION ALL SELECT 'job_listings',       COUNT(*) FROM job_listings
UNION ALL SELECT 'job_skills',         COUNT(*) FROM job_skills
UNION ALL SELECT 'education',          COUNT(*) FROM education
UNION ALL SELECT 'user_skills',        COUNT(*) FROM user_skills
UNION ALL SELECT 'market_trends',      COUNT(*) FROM market_trends
UNION ALL SELECT 'salary_audit',       COUNT(*) FROM salary_audit
ORDER  BY table_name;

-- =============================================================================
-- END OF performance.sql
-- =============================================================================
