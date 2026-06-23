-- =============================================================================
-- Career Intelligence and Job Analytics Platform
-- queries.sql — PostgreSQL 14+ · Live Demo Query Suite
-- Student: Rutvi Chirag Patel | ZDA24B008 | Z2004 DBMS
--
-- Run individually (highlight one block + execute) during your live demo.
-- Each query is labelled with its SQL category for the rubric.
-- =============================================================================


-- =============================================================================
-- Q01 — AGGREGATION
-- Average salary by employment type
-- =============================================================================
SELECT
    employment_type,
    COUNT(*)                              AS total_listings,
    ROUND(AVG(salary_min), 2)             AS avg_min_salary,
    ROUND(AVG(salary_max), 2)             AS avg_max_salary
FROM job_listings
WHERE salary_min IS NOT NULL AND salary_max IS NOT NULL
GROUP BY employment_type
ORDER BY avg_max_salary DESC;


-- =============================================================================
-- Q02 — AGGREGATION
-- Top 15 most in-demand skills across all job listings
-- =============================================================================
SELECT
    s.skill_name,
    s.category,
    COUNT(js.job_id) AS jobs_requiring_skill
FROM skills s
JOIN job_skills js ON s.skill_id = js.skill_id
GROUP BY s.skill_id, s.skill_name, s.category
ORDER BY jobs_requiring_skill DESC
LIMIT 15;


-- =============================================================================
-- Q03 — JOIN (3-table)
-- Active job listings with full company details
-- =============================================================================
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
ORDER BY jl.posted_date DESC
LIMIT 25;


-- =============================================================================
-- Q04 — JOIN (4-table)
-- Full user skill profile with education
-- =============================================================================
SELECT
    u.user_id,
    u.full_name,
    s.skill_name,
    us.proficiency_level,
    e.degree,
    e.field_of_study
FROM users u
JOIN user_skills us ON u.user_id = us.user_id
JOIN skills s       ON us.skill_id = s.skill_id
JOIN education e    ON u.user_id = e.user_id
ORDER BY u.user_id
LIMIT 25;


-- =============================================================================
-- Q05 — CORRELATED SUBQUERY  ★ Best Fit Jobs (core recommendation feature)
-- Jobs where the user holds ALL Required skills
-- Change user_id = 1 to demo for any user
-- =============================================================================
SELECT
    jl.job_id,
    jl.title,
    jl.salary_min,
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
            SELECT skill_id FROM user_skills WHERE user_id = 1
        )
  )
ORDER BY jl.salary_max DESC
LIMIT 20;


-- =============================================================================
-- Q06 — SCALAR SUBQUERY
-- Companies paying above the platform-wide average salary
-- =============================================================================
SELECT
    c.company_name,
    c.industry,
    ROUND(AVG(jl.salary_max), 2) AS avg_max_salary
FROM companies c
JOIN job_listings jl ON c.company_id = jl.company_id
WHERE jl.salary_max > (
    SELECT AVG(salary_max) FROM job_listings WHERE salary_max IS NOT NULL
)
GROUP BY c.company_id, c.company_name, c.industry
ORDER BY avg_max_salary DESC
LIMIT 20;


-- =============================================================================
-- Q07 — CTE  ★ Growth Jobs / Skill Gap Analysis
-- High-demand skills the user lacks, with trend direction
-- Change user_id = 5 to demo for any user
-- =============================================================================
WITH user_skills_held AS (
    SELECT skill_id FROM user_skills WHERE user_id = 5
),
market_demand AS (
    SELECT js.skill_id, COUNT(DISTINCT js.job_id) AS demand_count
    FROM job_skills js
    GROUP BY js.skill_id
    HAVING COUNT(DISTINCT js.job_id) >= 30
)
SELECT
    s.skill_name,
    s.category,
    md.demand_count,
    mt.avg_salary,
    mt.trend_direction
FROM market_demand md
JOIN skills s ON md.skill_id = s.skill_id
LEFT JOIN market_trends mt ON mt.skill_id = md.skill_id
    AND mt.recorded_month = (
        SELECT MAX(recorded_month) FROM market_trends WHERE skill_id = md.skill_id
    )
WHERE md.skill_id NOT IN (SELECT skill_id FROM user_skills_held)
ORDER BY md.demand_count DESC
LIMIT 10;


-- =============================================================================
-- Q08 — CHAINED CTEs
-- Year-on-year salary growth rate per skill
-- =============================================================================
WITH trend_base AS (
    SELECT skill_id, recorded_month, avg_salary,
           EXTRACT(YEAR FROM recorded_month)::INT AS trend_year
    FROM market_trends
),
trend_with_lag AS (
    SELECT skill_id, trend_year, avg_salary,
           LAG(avg_salary) OVER (PARTITION BY skill_id ORDER BY trend_year) AS prev_salary
    FROM trend_base
)
SELECT
    s.skill_name,
    t.trend_year,
    t.avg_salary,
    ROUND(((t.avg_salary - t.prev_salary) / t.prev_salary * 100)::NUMERIC, 2) AS yoy_growth_pct
FROM trend_with_lag t
JOIN skills s ON t.skill_id = s.skill_id
WHERE t.prev_salary IS NOT NULL
ORDER BY s.skill_name, t.trend_year;


-- =============================================================================
-- Q09 — WINDOW FUNCTION (RANK)
-- Rank job listings by salary within each company
-- =============================================================================
SELECT
    c.company_name,
    jl.title,
    jl.salary_max,
    RANK() OVER (PARTITION BY jl.company_id ORDER BY jl.salary_max DESC) AS salary_rank
FROM job_listings jl
JOIN companies c ON jl.company_id = c.company_id
WHERE jl.salary_max IS NOT NULL
ORDER BY c.company_name, salary_rank
LIMIT 25;


-- =============================================================================
-- Q10 — WINDOW FUNCTION (SUM OVER + ROW_NUMBER)
-- Running total of job listings posted per month
-- =============================================================================
SELECT
    employment_type,
    DATE_TRUNC('month', posted_date) AS posting_month,
    COUNT(*) AS listings_this_month,
    SUM(COUNT(*)) OVER (
        PARTITION BY employment_type
        ORDER BY DATE_TRUNC('month', posted_date)
    ) AS running_total
FROM job_listings
GROUP BY employment_type, DATE_TRUNC('month', posted_date)
ORDER BY employment_type, posting_month;


-- =============================================================================
-- ★ LIVE DEMO ORDER — run these 3 first if time is short:
--   1. Q05 (Best Fit Jobs)        — correlated subquery, core feature
--   2. Q07 (Growth Jobs)          — CTE, core feature
--   3. performance.sql Section 3 — EXPLAIN ANALYZE before/after index proof
-- =============================================================================
