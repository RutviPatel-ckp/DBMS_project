-- =============================================================================
-- load_data.sql — loads all 8 CSVs into PostgreSQL via \copy
-- Run this from the SAME directory that contains the data/ folder, e.g.:
--   cd ~/Desktop/pg_package
--   psql -d career_intelligence -f load_data.sql
--
-- \copy reads files from the CLIENT machine (your Mac), not the server,
-- so relative paths work fine as long as you run psql from the project root.
--
-- NOTE: \copy is a psql meta-command, not plain SQL — it must be written
-- as ONE continuous line with no backslash line-continuations.
-- =============================================================================

\copy users (user_id, full_name, email, location, years_experience, created_at) FROM 'data/users.csv' WITH (FORMAT csv, HEADER true, ENCODING 'utf-8');

\copy companies (company_id, company_name, industry, size_range, hq_location) FROM 'data/companies.csv' WITH (FORMAT csv, HEADER true, ENCODING 'utf-8');

\copy skills (skill_id, skill_name, category) FROM 'data/skills.csv' WITH (FORMAT csv, HEADER true, ENCODING 'utf-8');

\copy user_skills (user_skill_id, user_id, skill_id, proficiency_level, acquired_date) FROM 'data/user_skills.csv' WITH (FORMAT csv, HEADER true, ENCODING 'utf-8');

\copy education (education_id, user_id, institution, degree, field_of_study, graduation_year) FROM 'data/education.csv' WITH (FORMAT csv, HEADER true, ENCODING 'utf-8');

\copy job_listings (job_id, company_id, title, location, salary_min, salary_max, employment_type, description, posted_date, is_active) FROM 'data/job_listings.csv' WITH (FORMAT csv, HEADER true, ENCODING 'utf-8');

\copy job_skills (job_skill_id, job_id, skill_id, requirement_level) FROM 'data/job_skills.csv' WITH (FORMAT csv, HEADER true, ENCODING 'utf-8');

\copy market_trends (trend_id, skill_id, recorded_month, demand_score, avg_salary, trend_direction) FROM 'data/market_trends.csv' WITH (FORMAT csv, HEADER true, ENCODING 'utf-8');

-- ── Reset auto-increment sequences ──────────────────────────────────────────
-- Because we loaded explicit IDs from the CSVs, each table's identity
-- sequence still thinks the "next" ID is 1. This fixes that so any future
-- manual INSERT during Q&A doesn't collide with an existing ID.
SELECT setval(pg_get_serial_sequence('users', 'user_id'), COALESCE(MAX(user_id), 1)) FROM users;
SELECT setval(pg_get_serial_sequence('companies', 'company_id'), COALESCE(MAX(company_id), 1)) FROM companies;
SELECT setval(pg_get_serial_sequence('skills', 'skill_id'), COALESCE(MAX(skill_id), 1)) FROM skills;
SELECT setval(pg_get_serial_sequence('user_skills', 'user_skill_id'), COALESCE(MAX(user_skill_id), 1)) FROM user_skills;
SELECT setval(pg_get_serial_sequence('education', 'education_id'), COALESCE(MAX(education_id), 1)) FROM education;
SELECT setval(pg_get_serial_sequence('job_listings', 'job_id'), COALESCE(MAX(job_id), 1)) FROM job_listings;
SELECT setval(pg_get_serial_sequence('job_skills', 'job_skill_id'), COALESCE(MAX(job_skill_id), 1)) FROM job_skills;
SELECT setval(pg_get_serial_sequence('market_trends', 'trend_id'), COALESCE(MAX(trend_id), 1)) FROM market_trends;

-- ── Verify row counts ────────────────────────────────────────────────────────
SELECT 'users' AS table_name, COUNT(*) AS rows FROM users
UNION ALL SELECT 'companies', COUNT(*) FROM companies
UNION ALL SELECT 'skills', COUNT(*) FROM skills
UNION ALL SELECT 'user_skills', COUNT(*) FROM user_skills
UNION ALL SELECT 'education', COUNT(*) FROM education
UNION ALL SELECT 'job_listings', COUNT(*) FROM job_listings
UNION ALL SELECT 'job_skills', COUNT(*) FROM job_skills
UNION ALL SELECT 'market_trends', COUNT(*) FROM market_trends
ORDER BY table_name;
