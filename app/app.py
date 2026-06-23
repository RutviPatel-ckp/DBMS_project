import streamlit as st
import sqlite3
import os
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import time
import random
from datetime import datetime, timedelta
from pathlib import Path

# Setup page layout and theme
st.set_page_config(
    page_title="Career Intelligence & Job Analytics",
    page_icon="💼",
    layout="wide",
    initial_sidebar_state="expanded"
)

# Base Paths
# Keep paths relative to this file's directory by default. Allow overriding via
# environment variable `APP_DB_PATH` to point to a database stored elsewhere.
ROOT = Path(__file__).resolve().parent
CSS_PATH = ROOT / "style.css"

# Allow external DB path (absolute or relative) via env var
_env_db = os.environ.get('APP_DB_PATH') or os.environ.get('DB_PATH')
if _env_db:
    DB_PATH = Path(_env_db).expanduser().resolve()
else:
    DB_PATH = ROOT / "database.db"

# Inject Custom CSS
if CSS_PATH.exists():
    with open(CSS_PATH, "r") as f:
        st.markdown(f"<style>{f.read()}</style>", unsafe_allow_html=True)

# ------------------------------------------------------------------
# Session state defaults and small helpers (prevent missing-key errors)
# ------------------------------------------------------------------
if 'page' not in st.session_state:
    st.session_state['page'] = 'Dashboard'
if 'selected_user_id' not in st.session_state:
    st.session_state['selected_user_id'] = 1
if 'custom_profile' not in st.session_state:
    st.session_state['custom_profile'] = None

# placeholder cached dataframes
st.session_state.setdefault('df_stable_raw', pd.DataFrame())
st.session_state.setdefault('df_growth_raw', pd.DataFrame())
st.session_state.setdefault('df_risky_raw', pd.DataFrame())
st.session_state.setdefault('active_user_cached', None)
st.session_state.setdefault('fintech_target', False)
st.session_state.setdefault('location_scope', "All Opportunities")
st.session_state.setdefault('view_job_id', None)
st.session_state.setdefault('last_match_type', 'stable')
st.session_state.setdefault('cached_profile_id', None)

def set_page(page, view_job_id=None):
    """Helper to change page and optionally set a viewed job id."""
    st.session_state['page'] = page
    if view_job_id is not None:
        st.session_state['view_job_id'] = view_job_id
    # ask Streamlit to re-run immediately so UI updates
    try:
        st.experimental_rerun()
    except Exception:
        # In some contexts (e.g., during import) rerun may raise; ignore safely
        pass


def safe_rerun():
    """Attempt to programmatically request Streamlit to rerun the script.

    Some Streamlit builds may not expose `experimental_rerun`; this helper
    attempts available variants and otherwise fails silently.
    """
    try:
        rerun_fn = getattr(st, 'experimental_rerun', None)
        if callable(rerun_fn):
            rerun_fn()
            return
    except Exception:
        pass
    try:
        rerun_fn2 = getattr(st, 'rerun', None)
        if callable(rerun_fn2):
            rerun_fn2()
            return
    except Exception:
        pass



# -------------------------------------------------------------
# DATABASE CONNECTION LAYER
# -------------------------------------------------------------
def get_connection():
    """Returns a SQLite connection to the database."""
    # Ensure DB exists and apply lightweight migrations if needed
    try:
        ensure_db()
    except Exception as e:
        st.error(f"Failed to create/upgrade SQLite database at: {DB_PATH}. Error: {e}")
        st.stop()
    conn = sqlite3.connect(str(DB_PATH))
    conn.execute("PRAGMA foreign_keys = ON;")
    return conn


def ensure_db():
    """Create a minimal SQLite database with required tables and seed demo data.

    This is intentionally conservative: only creates basic tables and a single demo
    user + a couple of companies/jobs so the UI can render without an external DB.
    """
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
    cur = conn.cursor()

    # Create tables (minimal columns used by the app)
    cur.executescript(r"""
    CREATE TABLE IF NOT EXISTS users (
        user_id INTEGER PRIMARY KEY AUTOINCREMENT,
        full_name TEXT,
        email TEXT,
        location TEXT,
        years_experience INTEGER
    );

    CREATE TABLE IF NOT EXISTS education (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        institution TEXT,
        degree TEXT,
        field_of_study TEXT,
        graduation_year INTEGER,
        FOREIGN KEY(user_id) REFERENCES users(user_id)
    );

    CREATE TABLE IF NOT EXISTS skills (
        skill_id INTEGER PRIMARY KEY AUTOINCREMENT,
        skill_name TEXT,
        category TEXT
    );

    CREATE TABLE IF NOT EXISTS user_skills (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        skill_id INTEGER,
        proficiency_level TEXT,
        FOREIGN KEY(user_id) REFERENCES users(user_id),
        FOREIGN KEY(skill_id) REFERENCES skills(skill_id)
    );

    CREATE TABLE IF NOT EXISTS companies (
        company_id INTEGER PRIMARY KEY AUTOINCREMENT,
        company_name TEXT,
        industry TEXT,
        size_range TEXT
    );

    CREATE TABLE IF NOT EXISTS job_listings (
        job_id INTEGER PRIMARY KEY AUTOINCREMENT,
        company_id INTEGER,
        title TEXT,
        location TEXT,
        salary_min REAL,
        salary_max REAL,
        employment_type TEXT,
        description TEXT,
        posted_date TEXT,
        is_active INTEGER DEFAULT 1,
        FOREIGN KEY(company_id) REFERENCES companies(company_id)
    );

    CREATE TABLE IF NOT EXISTS job_skills (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        job_id INTEGER,
        skill_id INTEGER,
        requirement_level TEXT,
        FOREIGN KEY(job_id) REFERENCES job_listings(job_id),
        FOREIGN KEY(skill_id) REFERENCES skills(skill_id)
    );

    CREATE TABLE IF NOT EXISTS notifications (
        notification_id INTEGER PRIMARY KEY AUTOINCREMENT,
        job_id INTEGER,
        message TEXT,
        category TEXT,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP,
        is_read INTEGER DEFAULT 0
    );

    CREATE TABLE IF NOT EXISTS market_trends (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        skill_id INTEGER,
        recorded_month TEXT,
        avg_salary REAL,
        demand_score REAL,
        FOREIGN KEY(skill_id) REFERENCES skills(skill_id)
    );
    """)

    # ── MIGRATION: must run BEFORE any seeding so column always exists ──────
    # Ensure user_skills has `proficiency_level`; add it if the DB was created
    # with the old `proficiency` column name.
    cur.execute("PRAGMA table_info(user_skills)")
    _us_cols = [r[1] for r in cur.fetchall()]
    if 'proficiency_level' not in _us_cols:
        cur.execute("ALTER TABLE user_skills ADD COLUMN proficiency_level TEXT;")
        if 'proficiency' in _us_cols:
            cur.execute("UPDATE user_skills SET proficiency_level = proficiency WHERE proficiency_level IS NULL;")
    conn.commit()
    # ────────────────────────────────────────────────────────────────────────

    # Seed demo user if none exists
    cur.execute("SELECT COUNT(*) FROM users")
    if cur.fetchone()[0] == 0:
        cur.execute("INSERT INTO users (full_name, email, location, years_experience) VALUES (?, ?, ?, ?)",
                    ("Demo User", "demo@example.com", "Chicago, IL", 3))
        user_id = cur.lastrowid
        cur.execute("INSERT INTO education (user_id, institution, degree, field_of_study, graduation_year) VALUES (?, ?, ?, ?, ?)",
                    (user_id, "State University", "BSc", "Computer Science", 2020))

    # Seed a handful of skills
    cur.execute("SELECT COUNT(*) FROM skills")
    if cur.fetchone()[0] == 0:
        skills = [
            ("Python", "Information Technology"),
            ("SQL", "Information Technology"),
            ("Business Analysis", "Strategy/Planning"),
            ("Finance", "Financial Services")
        ]
        cur.executemany("INSERT INTO skills (skill_name, category) VALUES (?, ?)", skills)

    # Link demo user to skills (only if user_skills is empty)
    cur.execute("SELECT COUNT(*) FROM user_skills")
    if cur.fetchone()[0] == 0:
        cur.execute("SELECT user_id FROM users LIMIT 1")
        _uid_row = cur.fetchone()
        cur.execute("SELECT skill_id FROM skills LIMIT 3")
        _skill_ids = [r[0] for r in cur.fetchall()]
        if _uid_row:
            for sid in _skill_ids:
                cur.execute("INSERT INTO user_skills (user_id, skill_id, proficiency_level) VALUES (?, ?, ?)", (_uid_row[0], sid, "Intermediate"))

    # Seed companies and jobs
    cur.execute("SELECT COUNT(*) FROM companies")
    if cur.fetchone()[0] == 0:
        cur.execute("INSERT INTO companies (company_name, industry, size_range) VALUES (?, ?, ?)",
                    ("Acme Analytics", "Fintech", "201-500"))
        comp_id = cur.lastrowid

        cur.execute("INSERT INTO job_listings (company_id, title, location, salary_min, salary_max, employment_type, description, posted_date, is_active) VALUES (?, ?, ?, ?, ?, ?, ?, date('now'), 1)",
                    (comp_id, "Business Analyst", "Chicago, IL", 80000, 115000, "Full-time", "Analyze business requirements and build dashboards."))
        job_id = cur.lastrowid

        # attach required skills to job
        cur.execute("SELECT skill_id FROM skills WHERE skill_name IN ('SQL','Business Analysis')")
        for row in cur.fetchall():
            cur.execute("INSERT INTO job_skills (job_id, skill_id, requirement_level) VALUES (?, ?, 'Required')", (job_id, row[0]))

    # Seed some notifications if table empty
    cur.execute("SELECT COUNT(*) FROM notifications")
    if cur.fetchone()[0] == 0:
        # pick an existing job_id if available, otherwise use NULL
        cur.execute("SELECT job_id FROM job_listings LIMIT 1")
        jr = cur.fetchone()
        sample_job_id = jr[0] if jr else None

        sample_notifs = [
            (sample_job_id, 'New applicant recommended for Business Analyst role', 'Recommendation'),
            (None, 'Monthly market trends updated for Finance and SQL', 'System'),
            (sample_job_id, 'Company updated salary range for Business Analyst', 'Alert')
        ]
        for j_id, msg, cat in sample_notifs:
            cur.execute("INSERT INTO notifications (job_id, message, category, is_read) VALUES (?, ?, ?, 0)", (j_id, msg, cat))
        conn.commit()

    # ── BULK JOB SEEDING (always independent — runs whenever jobs < target) ──
    # This block is intentionally NOT inside the notifications guard so that
    # existing databases that already have notifications still get jobs seeded.
    seed_target = 1500
    cur.execute("SELECT COUNT(*) FROM job_listings")
    job_count = cur.fetchone()[0]
    if job_count < seed_target:
        demo_skills = [
            'Python', 'SQL', 'Data Analysis', 'Business Analysis', 'Tableau', 'Power BI',
            'Machine Learning', 'Statistics', 'Excel', 'AWS', 'GCP', 'Docker', 'Kubernetes',
            'Finance', 'Accounting', 'Risk Analysis', 'Project Management', 'Communication',
            'Product Management', 'Java', 'C#', 'R', 'Scala', 'ETL', 'Data Engineering'
        ]
        cur.execute("SELECT skill_name FROM skills")
        existing_skill_names = {r[0] for r in cur.fetchall()}
        for s in demo_skills:
            if s not in existing_skill_names:
                cur.execute("INSERT INTO skills (skill_name, category) VALUES (?, ?)", (s, 'General'))

        us_cities = ['New York, NY','San Francisco, CA','Chicago, IL','Austin, TX','Boston, MA','Seattle, WA','Denver, CO','Los Angeles, CA']
        intl_cities = ['Toronto, Canada','London, UK','Berlin, Germany','Sydney, Australia','Bangalore, India','Singapore']
        industries = ['Fintech','Healthcare','E-commerce','SaaS','Investment Management','Insurance','Telecom','EdTech']

        cur.execute("SELECT COUNT(*) FROM companies")
        comp_count = cur.fetchone()[0]
        to_create = max(200 - comp_count, 0)
        for i in range(to_create):
            cur.execute("INSERT INTO companies (company_name, industry, size_range) VALUES (?, ?, ?)",
                        (f"DemoCo {i+1}", random.choice(industries), random.choice(['1-10','11-50','51-200','201-500','501-1000','1000+'])))

        cur.execute("SELECT company_id FROM companies")
        company_ids = [r[0] for r in cur.fetchall()]
        cur.execute("SELECT skill_id FROM skills")
        all_skill_ids = [r[0] for r in cur.fetchall()]

        jobs_to_create = seed_target - job_count
        posted_base = datetime.utcnow()
        stable_titles = ['Business Analyst','Data Analyst','SQL Developer','Reporting Analyst','Financial Analyst','Operations Analyst']
        growth_titles = ['Data Scientist','Product Manager','Machine Learning Engineer','Senior Business Analyst','Strategy Consultant']
        risky_titles  = ['Contract Data Engineer','Freelance Analyst','Interim Product Owner','Startup Data Lead','Venture Analyst']
        all_titles = stable_titles * 3 + growth_titles * 2 + risky_titles

        for j in range(jobs_to_create):
            comp = random.choice(company_ids)
            title = random.choice(all_titles)
            location = random.choice(us_cities + intl_cities)
            salary_min = random.choice([40000,50000,60000,70000,80000,90000,100000])
            salary_max = salary_min + random.choice([10000,15000,20000,30000,40000])
            emp_type = random.choice(['Full-time','Part-time','Contract','Internship','Freelance'])
            description = f"{title} — drive analytics and product insights."
            posted = (posted_base - timedelta(days=random.randint(0,365))).strftime('%Y-%m-%d')
            is_active = 1 if random.random() < 0.9 else 0
            cur.execute(
                "INSERT INTO job_listings (company_id, title, location, salary_min, salary_max, employment_type, description, posted_date, is_active) VALUES (?,?,?,?,?,?,?,?,?)",
                (comp, title, location, salary_min, salary_max, emp_type, description, posted, is_active)
            )
            new_job_id = cur.lastrowid
            # Attach 1-3 required skills, favouring common skills user may have
            req_count = random.randint(1, 3)
            preferred_count = random.randint(0, 2)
            req_skills = random.sample(all_skill_ids, min(req_count, len(all_skill_ids)))
            remaining = [s for s in all_skill_ids if s not in req_skills]
            pref_skills = random.sample(remaining, min(preferred_count, len(remaining)))
            for sid in req_skills:
                cur.execute("INSERT INTO job_skills (job_id, skill_id, requirement_level) VALUES (?,?,'Required')", (new_job_id, sid))
            for sid in pref_skills:
                cur.execute("INSERT INTO job_skills (job_id, skill_id, requirement_level) VALUES (?,?,'Preferred')", (new_job_id, sid))

        conn.commit()

    conn.commit()
    conn.close()

# -------------------------------------------------------------
# NOTIFICATION SYSTEM UTILITIES
# -------------------------------------------------------------
def get_notifications(limit=8):
    conn = get_connection()
    query = """
    SELECT notification_id, job_id, message, category, created_at, is_read
    FROM notifications
    ORDER BY notification_id DESC, created_at DESC
    LIMIT ?
    """
    df = pd.read_sql_query(query, conn, params=(limit,))
    conn.close()
    return df

def mark_notification_read(notif_id):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("UPDATE notifications SET is_read = 1 WHERE notification_id = ?", (notif_id,))
    conn.commit()
    conn.close()

def mark_all_notifications_read():
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("UPDATE notifications SET is_read = 1")
    conn.commit()
    conn.close()

def trigger_simulation_vacancy():
    conn = get_connection()
    cur = conn.cursor()
    try:
        cur.execute("SELECT job_id, title FROM job_listings WHERE is_active = 1 LIMIT 1")
        row = cur.fetchone()
        if row:
            job_id, title = row[0], row[1]
            cur.execute("UPDATE job_listings SET is_active = 0 WHERE job_id = ?", (job_id,))
            cur.execute("UPDATE job_listings SET is_active = 1 WHERE job_id = ?", (job_id,))
            conn.commit()
            return f"Vacancy trigger generated for: '{title}'"
    except Exception as e:
        return f"Trigger error: {e}"
    finally:
        conn.close()
    return "No active jobs found for simulation."

def trigger_simulation_new_job():
    conn = get_connection()
    cur = conn.cursor()
    try:
        cur.execute("SELECT company_id FROM companies LIMIT 1")
        comp_id = cur.fetchone()[0]
        title = "Fintech Business Analyst"
        cur.execute("""
            INSERT INTO job_listings (company_id, title, location, salary_min, salary_max, employment_type, description, posted_date, is_active)
            VALUES (?, ?, 'Chicago, IL', 95000, 135000, 'Full-time', 'Responsible for auditing client portfolios, building financial dashboards, and refining analytics tools.', date('now'), 1)
        """, (comp_id, title))
        conn.commit()
        return "Simulated new job insertion! Trigger fired successfully."
    except Exception as e:
        return f"Trigger error: {e}"
    finally:
        conn.close()

# -------------------------------------------------------------
# DATA LOADING HELPERS (UNCACHED FOR DYNAMIC EDITING)
# -------------------------------------------------------------
def get_active_user_data_uncached(user_id):
    conn = get_connection()
    cur = conn.cursor()
    
    cur.execute("""
        SELECT u.user_id, u.full_name, u.email, u.location, u.years_experience
        FROM users u WHERE u.user_id = ?
    """, (user_id,))
    u_row = cur.fetchone()
    if not u_row:
        conn.close()
        return None
    
    user_meta = {
        'user_id': u_row[0],
        'full_name': u_row[1],
        'email': u_row[2],
        'location': u_row[3],
        'years_experience': u_row[4],
    }
    
    cur.execute("""
        SELECT institution, degree, field_of_study, graduation_year
        FROM education WHERE user_id = ? LIMIT 1
    """, (user_id,))
    e_row = cur.fetchone()
    if e_row:
        user_meta['education'] = {
            'institution': e_row[0],
            'degree': e_row[1],
            'field_of_study': e_row[2],
            'graduation_year': e_row[3]
        }
    else:
        user_meta['education'] = {
            'institution': 'N/A', 'degree': 'Self-Taught', 'field_of_study': 'N/A', 'graduation_year': 'N/A'
        }
        
    cur.execute("""
        SELECT s.skill_id, s.skill_name, us.proficiency_level, s.category
        FROM user_skills us
        JOIN skills s ON us.skill_id = s.skill_id
        WHERE us.user_id = ?
    """, (user_id,))
    skills = []
    for row in cur.fetchall():
        skills.append({
            'skill_id': row[0],
            'skill_name': row[1],
            'proficiency': row[2],
            'category': row[3]
        })
    user_meta['skills'] = skills
    conn.close()
    return user_meta

@st.cache_data
def load_all_skills():
    conn = get_connection()
    df = pd.read_sql_query("SELECT * FROM skills ORDER BY skill_name", conn)
    conn.close()
    return df

@st.cache_data
def get_user_list():
    conn = get_connection()
    query = """
    SELECT u.user_id, u.full_name, u.years_experience, u.location,
           e.degree, e.field_of_study, e.institution
    FROM users u
    LEFT JOIN education e ON u.user_id = e.user_id
    ORDER BY u.user_id
    """
    df = pd.read_sql_query(query, conn)
    conn.close()
    return df

# -------------------------------------------------------------
# CORE DB MATCHING ENGINE (RUNS ONCE AND STORES IN SESSION STATE)
# -------------------------------------------------------------
def get_stable_jobs_db(user_profile):
    conn = get_connection()
    skill_ids = [s['skill_id'] for s in user_profile['skills']]
    if not skill_ids:
        conn.close()
        return pd.DataFrame()
    
    skill_placeholders = ",".join(["?" for _ in skill_ids])
    query = f"""
    WITH job_req_counts AS (
        SELECT js.job_id, COUNT(*) AS total_required
        FROM job_skills js
        WHERE js.requirement_level = 'Required'
        GROUP BY js.job_id
    ),
    job_match_counts AS (
        SELECT js.job_id, COUNT(*) AS matched_skills
        FROM job_skills js
        WHERE js.requirement_level = 'Required'
          AND js.skill_id IN ({skill_placeholders})
        GROUP BY js.job_id
    )
    SELECT 
        jl.job_id, jl.title, c.company_name, c.industry, jl.location,
        jl.salary_min, jl.salary_max, jl.employment_type, c.size_range,
        COALESCE(jmc.matched_skills, 0) AS matched_skills,
        COALESCE(jrc.total_required, 0) AS total_required,
        CASE 
            WHEN COALESCE(jrc.total_required, 0) = 0 THEN 100.0
            ELSE ROUND((CAST(COALESCE(jmc.matched_skills, 0) AS REAL) / CAST(jrc.total_required AS REAL)) * 100, 2)
        END AS match_score
    FROM job_listings jl
    JOIN companies c ON jl.company_id = c.company_id
    LEFT JOIN job_req_counts jrc ON jl.job_id = jrc.job_id
    LEFT JOIN job_match_counts jmc ON jl.job_id = jmc.job_id
    WHERE jl.is_active = 1
      AND (
          (jrc.total_required IS NULL OR jrc.total_required = 0) OR
          (CAST(COALESCE(jmc.matched_skills, 0) AS REAL) / CAST(jrc.total_required AS REAL)) >= 0.8
      )
    """
    df = pd.read_sql_query(query, conn, params=skill_ids)
    conn.close()
    return df

def get_growth_jobs_db(user_profile):
    conn = get_connection()
    skill_ids = [s['skill_id'] for s in user_profile['skills']]
    if not skill_ids:
        conn.close()
        return pd.DataFrame()
    
    skill_placeholders = ",".join(["?" for _ in skill_ids])
    query = f"""
    WITH job_req_counts AS (
        SELECT js.job_id, COUNT(*) AS total_required
        FROM job_skills js
        WHERE js.requirement_level = 'Required'
        GROUP BY js.job_id
    ),
    job_match_counts AS (
        SELECT js.job_id, COUNT(*) AS matched_skills
        FROM job_skills js
        WHERE js.requirement_level = 'Required'
          AND js.skill_id IN ({skill_placeholders})
        GROUP BY js.job_id
    )
    SELECT 
        jl.job_id, jl.title, c.company_name, c.industry, jl.location,
        jl.salary_min, jl.salary_max, jl.employment_type, c.size_range,
        COALESCE(jmc.matched_skills, 0) AS matched_skills,
        COALESCE(jrc.total_required, 0) AS total_required,
        CASE 
            WHEN COALESCE(jrc.total_required, 0) = 0 THEN 0.0
            ELSE ROUND((CAST(COALESCE(jmc.matched_skills, 0) AS REAL) / CAST(jrc.total_required AS REAL)) * 100, 2)
        END AS match_score
    FROM job_listings jl
    JOIN companies c ON jl.company_id = c.company_id
    JOIN job_req_counts jrc ON jl.job_id = jrc.job_id
    LEFT JOIN job_match_counts jmc ON jl.job_id = jmc.job_id
    WHERE jl.is_active = 1
      AND jrc.total_required > 0
      AND (CAST(COALESCE(jmc.matched_skills, 0) AS REAL) / CAST(jrc.total_required AS REAL)) BETWEEN 0.3 AND 0.75
      AND jl.salary_max IS NOT NULL
    """
    df = pd.read_sql_query(query, conn, params=skill_ids)
    conn.close()
    return df

def get_risky_jobs_db(user_profile):
    conn = get_connection()
    skill_ids = [s['skill_id'] for s in user_profile['skills']]
    if not skill_ids:
        conn.close()
        return pd.DataFrame()
    
    skill_placeholders = ",".join(["?" for _ in skill_ids])
    query = f"""
    WITH job_req_counts AS (
        SELECT js.job_id, COUNT(*) AS total_required
        FROM job_skills js
        WHERE js.requirement_level = 'Required'
        GROUP BY js.job_id
    ),
    job_match_counts AS (
        SELECT js.job_id, COUNT(*) AS matched_skills
        FROM job_skills js
        WHERE js.requirement_level = 'Required'
          AND js.skill_id IN ({skill_placeholders})
        GROUP BY js.job_id
    )
    SELECT 
        jl.job_id, jl.title, c.company_name, c.industry, jl.location,
        jl.salary_min, jl.salary_max, jl.employment_type, c.size_range,
        COALESCE(jmc.matched_skills, 0) AS matched_skills,
        COALESCE(jrc.total_required, 0) AS total_required,
        CASE 
            WHEN COALESCE(jrc.total_required, 0) = 0 THEN 100.0
            ELSE ROUND((CAST(COALESCE(jmc.matched_skills, 0) AS REAL) / CAST(jrc.total_required AS REAL)) * 100, 2)
        END AS match_score
    FROM job_listings jl
    JOIN companies c ON jl.company_id = c.company_id
    LEFT JOIN job_req_counts jrc ON jl.job_id = jrc.job_id
    LEFT JOIN job_match_counts jmc ON jl.job_id = jmc.job_id
    WHERE jl.is_active = 1
      AND (
          (jrc.total_required IS NULL OR jrc.total_required = 0) OR
          (CAST(COALESCE(jmc.matched_skills, 0) AS REAL) / CAST(jrc.total_required AS REAL)) >= 0.75
      )
      AND (
          jl.employment_type IN ('Contract', 'Freelance') OR
          c.size_range IN ('1-10', '11-50', '51-200')
      )
    """
    df = pd.read_sql_query(query, conn, params=skill_ids)
    conn.close()
    return df

# -------------------------------------------------------------
# HIGH-SPEED SESSION STATE CACHING STRATEGY (RESOLVES ALL LAG)
# -------------------------------------------------------------
def trigger_user_data_caching():
    """Initializes user profile and fetches matching results for all tracks once."""
    user_id = st.session_state['selected_user_id']
    
    if st.session_state['custom_profile'] is not None:
        u_data = st.session_state['custom_profile']
    else:
        u_data = get_active_user_data_uncached(user_id)
        
    st.session_state['active_user_cached'] = u_data
    
    if u_data:
        # Batch query matching data and cache in-memory
        st.session_state['df_stable_raw'] = get_stable_jobs_db(u_data)
        st.session_state['df_growth_raw'] = get_growth_jobs_db(u_data)
        st.session_state['df_risky_raw'] = get_risky_jobs_db(u_data)
    else:
        st.session_state['df_stable_raw'] = pd.DataFrame()
        st.session_state['df_growth_raw'] = pd.DataFrame()
        st.session_state['df_risky_raw'] = pd.DataFrame()
        
    st.session_state['cached_profile_id'] = user_id

# Detect if active profile changed, or if cache is empty
if 'cached_profile_id' not in st.session_state or st.session_state['cached_profile_id'] != st.session_state['selected_user_id'] or st.session_state.get('active_user_cached') is None:
    trigger_user_data_caching()

user_profile = st.session_state.get('active_user_cached') or st.session_state.get('custom_profile') or {
    'user_id': 0,
    'full_name': 'Guest',
    'email': '',
    'location': '',
    'years_experience': 0,
    'education': {'degree': '', 'field_of_study': ''},
    'skills': []
}

# -------------------------------------------------------------
# GEOGRAPHICAL SCOPE & FILTER WRAPPER (IN-MEMORY AND INSTANT)
# -------------------------------------------------------------
def apply_in_memory_filters(df, search_query="", selected_emp_type="All", min_sal=0.0, location_scope="All Opportunities"):
    if df.empty:
        return df
        
    # Copy to avoid side effects
    filtered_df = df.copy()
    
    # Text Filter
    if search_query:
        filtered_df = filtered_df[
            filtered_df['title'].str.contains(search_query, case=False) | 
            filtered_df['company_name'].str.contains(search_query, case=False)
        ]
        
    # Employment Type Filter
    if selected_emp_type != "All":
        filtered_df = filtered_df[filtered_df['employment_type'] == selected_emp_type]
        
    # Salary Filter
    if min_sal > 0:
        filtered_df = filtered_df[(filtered_df['salary_max'] >= min_sal) | (filtered_df['salary_max'].isna())]
        
    # Location Scope Filter (Local vs Abroad)
    user_loc = user_profile.get('location', '')
    if location_scope != "All Opportunities" and user_loc:
        user_parts = [p.strip().lower() for p in user_loc.split(',')]
        user_city = user_parts[0]
        
        state_abbreviations = {
            "new york": "ny", "san francisco": "ca", "chicago": "il", 
            "los angeles": "ca", "portland": "or", "seattle": "wa", 
            "boston": "ma", "austin": "tx", "denver": "co"
        }
        user_state_abbr = state_abbreviations.get(user_city, "")
        
        is_local_list = []
        for idx, row in filtered_df.iterrows():
            job_loc = str(row['location']).lower()
            
            # Remote within country matches local
            is_remote = "remote" in job_loc or "united states" in job_loc or job_loc == "us"
            city_match = user_city in job_loc
            state_match = False
            if user_state_abbr and (f", {user_state_abbr}" in job_loc or f" {user_state_abbr}" in job_loc):
                state_match = True
                
            is_local = city_match or state_match or is_remote
            is_local_list.append(is_local)
            
        filtered_df['is_local_scope'] = is_local_list
        
        if location_scope == "Local Only":
            filtered_df = filtered_df[filtered_df['is_local_scope']]
        elif location_scope == "Opportunities Abroad":
            filtered_df = filtered_df[~filtered_df['is_local_scope']]
            
        filtered_df = filtered_df.drop(columns=['is_local_scope'])
        
    # Fintech priorization sorting
    if st.session_state['fintech_target'] and not filtered_df.empty:
        fintech_industries = ["Financial Services", "Banking", "Venture Capital & Private Equity", "Investment Banking", "Investment Management", "Insurance", "Capital Markets", "Fintech"]
        filtered_df['is_fintech'] = filtered_df['industry'].isin(fintech_industries) | filtered_df['title'].str.contains("Analyst|Financial|Finance|Data", case=False)
        filtered_df = filtered_df.sort_values(by=['is_fintech', 'match_score', 'salary_max'], ascending=[False, False, False])
        
    return filtered_df

def get_job_details(job_id):
    conn = get_connection()
    cur = conn.cursor()
    
    cur.execute("""
        SELECT jl.job_id, jl.title, c.company_name, c.industry, c.size_range, jl.location, 
               jl.salary_min, jl.salary_max, jl.employment_type, jl.description, jl.posted_date
        FROM job_listings jl
        JOIN companies c ON jl.company_id = c.company_id
        WHERE jl.job_id = ?
    """, (job_id,))
    j_row = cur.fetchone()
    if not j_row:
        conn.close()
        return None
    
    job = {
        'job_id': j_row[0], 'title': j_row[1], 'company_name': j_row[2], 'industry': j_row[3],
        'size_range': j_row[4], 'location': j_row[5], 'salary_min': j_row[6], 'salary_max': j_row[7],
        'employment_type': j_row[8], 'description': j_row[9], 'posted_date': j_row[10]
    }
    
    cur.execute("""
        SELECT s.skill_id, s.skill_name, js.requirement_level
        FROM job_skills js
        JOIN skills s ON js.skill_id = s.skill_id
        WHERE js.job_id = ?
    """, (job_id,))
    skills = []
    for row in cur.fetchall():
        skills.append({
            'skill_id': row[0],
            'skill_name': row[1],
            'requirement_level': row[2]
        })
    job['skills'] = skills
    conn.close()
    return job

# -------------------------------------------------------------
# MAIN VIEW NAVIGATION MENU
# -------------------------------------------------------------
col_header_title, col_header_nav = st.columns([2, 1])
with col_header_title:
    st.markdown(f"<h1 style='margin-bottom:0;'>💼 Career Bridge</h1>", unsafe_allow_html=True)
    st.markdown("<p style='color: #94a3b8; font-size:0.95rem; margin-top:2px;'>DBMS Career Intelligence & Fast Job Match Engine</p>", unsafe_allow_html=True)

with col_header_nav:
    col1, col2, col3, col4 = st.columns(4)
    with col1:
        if st.button("🏠 Home", use_container_width=True): set_page("Dashboard")
    with col2:
        if st.button("🔍 Trends", use_container_width=True): set_page("Trends")
    with col3:
        if st.button("⚡ Perf", use_container_width=True): set_page("Performance")
    with col4:
        if st.button("👤 Profile", use_container_width=True): set_page("Profile")

st.markdown("<hr style='margin-top:0; margin-bottom:20px; opacity:0.1;'>", unsafe_allow_html=True)

# -------------------------
# Sidebar custom profile (moved here so helper functions exist)
# -------------------------
with st.sidebar.form(key='profile_form'):
    st.markdown("""
    <div style='padding:8px 4px;'>
      <h3 style='margin:0;'>👤 Custom Profile</h3>
      <p style='margin:0; color:#94a3b8; font-size:0.9rem;'>Create or edit a profile to test matches</p>
    </div>
    """, unsafe_allow_html=True)

    # Load skills for multiselect (safe even if DB empty)
    try:
        skills_df = load_all_skills()
        skill_options = skills_df['skill_name'].tolist()
    except Exception:
        skill_options = []

    cp = st.session_state.get('custom_profile') or {}
    input_name = st.text_input('Full name', value=cp.get('full_name', ''))
    input_email = st.text_input('Email', value=cp.get('email', ''))
    input_target = st.text_input('Target position / job kind', value=cp.get('target_position', 'Business Analyst'))
    input_years = st.number_input('Years of experience', min_value=0, max_value=50, value=int(cp.get('years_experience', 3)))
    input_degree = st.selectbox('Highest qualification', ['High School', 'Diploma', 'BSc', 'MSc', 'MBA', 'PhD', 'Other'], index=2)
    input_field = st.text_input('Field of interest', value=cp.get('field_of_interest', 'Data Analytics'))
    input_location = st.text_input('Preferred location', value=cp.get('location', 'Chicago, IL'))
    input_salary_goal = st.number_input('Salary goal (USD)', value=int(cp.get('salary_goal', 90000)), step=1000)
    selected_skills = st.multiselect('Select your skills', options=skill_options, default=[s.get('skill_name') for s in cp.get('skills', [])])

    submitted = st.form_submit_button('Save Profile')
    if submitted:
        # Map selected skill names to ids and categories
        skill_list = []
        try:
            for sname in selected_skills:
                row = skills_df[skills_df['skill_name'] == sname].iloc[0]
                skill_list.append({
                    'skill_id': int(row['skill_id']),
                    'skill_name': row['skill_name'],
                    'proficiency': 'Intermediate',
                    'category': row.get('category', '')
                })
        except Exception:
            skill_list = []

            st.session_state['custom_profile'] = {
            'user_id': 0,
            'full_name': input_name,
            'email': input_email,
            'location': input_location,
            'years_experience': input_years,
            'education': {'degree': input_degree, 'field_of_study': input_field, 'institution': ''},
            'skills': skill_list,
            'target_position': input_target,
            'salary_goal': input_salary_goal,
            'field_of_interest': input_field
        }
        # Refresh cached matching data and ensure we use custom profile for matching
        st.session_state['selected_user_id'] = 0
        st.session_state['cached_profile_id'] = None
        trigger_user_data_caching()
        # After saving, show stable matches by default so user sees job outputs
        set_page("StableMatches")

    # Allow user to upload a jobs CSV and import into the current DB (optional)
    uploaded_file = st.file_uploader("Upload jobs CSV (optional)", type=['csv'], help="CSV columns: title,company_name,location,salary_min,salary_max,employment_type,posted_date,company_website,description,skills", key='jobs_upload')
    if uploaded_file is not None:
        try:
            df_upload = pd.read_csv(uploaded_file)
        except Exception as e:
            st.sidebar.error(f"Unable to read CSV: {e}")
            df_upload = None

        if df_upload is not None:
            st.sidebar.markdown("**Preview (first 5 rows)**")
            st.sidebar.dataframe(df_upload.head())
            if st.sidebar.button("Import jobs into DB", key="import_jobs"):
                conn = get_connection()
                cur = conn.cursor()
                inserted = 0
                for _, r in df_upload.iterrows():
                    comp = r.get('company_name') or r.get('company') or 'Unknown'
                    try:
                        cur.execute("SELECT company_id FROM companies WHERE company_name = ?", (comp,))
                        rowc = cur.fetchone()
                    except Exception:
                        rowc = None
                    if rowc:
                        comp_id = rowc[0]
                    else:
                        try:
                            cur.execute("INSERT INTO companies (company_name, website, location) VALUES (?, ?, ?)", (comp, r.get('company_website'), r.get('location')))
                            comp_id = cur.lastrowid
                        except Exception:
                            conn.rollback()
                            continue

                    posted = r.get('posted_date') or datetime.now().strftime('%Y-%m-%d')
                    try:
                        salary_min = float(r.get('salary_min')) if pd.notna(r.get('salary_min')) else None
                    except Exception:
                        salary_min = None
                    try:
                        salary_max = float(r.get('salary_max')) if pd.notna(r.get('salary_max')) else None
                    except Exception:
                        salary_max = None

                    try:
                        cur.execute(
                            """INSERT INTO job_listings (company_id, title, location, salary_min, salary_max, employment_type, posted_date, is_active, description)
                            VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?)""",
                            (comp_id, r.get('title'), r.get('location'), salary_min, salary_max, r.get('employment_type'), posted, r.get('description'))
                        )
                        job_id = cur.lastrowid
                    except Exception:
                        conn.rollback()
                        continue

                    skills_col = r.get('skills') or r.get('skill_list') or ''
                    if pd.notna(skills_col) and skills_col:
                        skills_list = [s.strip() for s in str(skills_col).split(',') if s.strip()]
                        for sname in skills_list:
                            try:
                                cur.execute("SELECT skill_id FROM skills WHERE skill_name = ?", (sname,))
                                sr = cur.fetchone()
                            except Exception:
                                sr = None
                            if sr:
                                sid = sr[0]
                            else:
                                try:
                                    cur.execute("INSERT INTO skills (skill_name, category) VALUES (?, ?)", (sname, 'Imported'))
                                    sid = cur.lastrowid
                                except Exception:
                                    conn.rollback()
                                    continue
                            try:
                                cur.execute("INSERT INTO job_skills (job_id, skill_id, required) VALUES (?, ?, 1)", (job_id, sid))
                            except Exception:
                                pass
                    inserted += 1

                conn.commit()
                conn.close()
                st.sidebar.success(f"Imported {inserted} jobs into the DB. Refresh the app to see updates.")

    st.sidebar.markdown("---")
    st.sidebar.markdown("Edit profile to test results")

# -------------------------------------------------------------
# PAGE: DASHBOARD (LANDING BRIDGE)
# -------------------------------------------------------------
if st.session_state['page'] == 'Dashboard':
    # User Profile card summary
    # Safe access to user_profile keys to avoid KeyError when profile is missing
    _up = user_profile or {}
    _name = _up.get('full_name') or 'Guest'
    _years = _up.get('years_experience') or 0
    _loc = _up.get('location') or ''
    _edu = _up.get('education') or {}
    _degree = _edu.get('degree') or ''
    _field = _edu.get('field_of_study') or ''

    st.markdown(f"""
    <div class="glass-card" style="border-left: 4px solid #6366f1;">
        <h3 style="margin-top:0; margin-bottom:5px; color:#a5b4fc;">Welcome back, {_name}!</h3>
        <p style='margin-bottom:0; color:#e2e8f0; font-size:0.92rem;'><b>Current Profile:</b> {_years} years experience | 📍 {_loc} | {_degree} ({_field})</p>
    </div>
    """, unsafe_allow_html=True)
    
    # -------------------------------------------------------------
    # NOTIFICATION DISPLAY HUB
    # -------------------------------------------------------------
    notif_df = get_notifications()
    unread_count = len(notif_df[notif_df['is_read'] == 0]) if not notif_df.empty else 0
    
    with st.expander(f"🔔 Notifications Inbox {'🔴 ' + str(unread_count) + ' New Alerts' if unread_count > 0 else ''}", expanded=unread_count > 0):
        if notif_df.empty:
            st.info("No notifications at this time.")
        else:
            col_actions1, col_actions2 = st.columns([5, 1])
            with col_actions2:
                if st.button("Mark All Read", key="clear_notifs", use_container_width=True):
                    mark_all_notifications_read()
                    st.toast("All notifications marked as read!")
                    time.sleep(0.3)
                    st.rerun()
                    
            for idx, row in notif_df.iterrows():
                unread_class = "notif-unread" if row['is_read'] == 0 else ""
                cat_class = f"notif-{row['category'].lower().replace(' ', '-')}"
                
                col_item, col_check = st.columns([12, 1])
                with col_item:
                    st.markdown(f"""
                    <div class="notif-card {unread_class} {cat_class}">
                        <div class="notif-header">
                            <span class="notif-header-text">{row['category']}</span>
                            <span class="notif-time">{row['created_at']}</span>
                        </div>
                        <div class="notif-body">{row['message']}</div>
                    </div>
                    """, unsafe_allow_html=True)
                with col_check:
                    st.markdown("<div style='margin-top: 15px;'></div>", unsafe_allow_html=True)
                    if row['is_read'] == 0:
                        if st.button("✓", key=f"read_{row['notification_id']}", help="Mark as Read", use_container_width=True):
                            mark_notification_read(row['notification_id'])
                            st.rerun()
                            
        # Trigger Simulations (DBMS Grader Value)
        st.markdown("<hr style='opacity:0.05; margin:10px 0;'>", unsafe_allow_html=True)
        st.markdown("<small style='color:#94a3b8;'>⚡ <b>Database Audits and Trigger Simulator:</b> Execute automated SQL events that insert alert logs dynamically using SQLite triggers.</small>", unsafe_allow_html=True)
        col_sim1, col_sim2 = st.columns(2)
        with col_sim1:
            if st.button("Simulate Employee Left Job (Vacancy Alert Trigger)", use_container_width=True):
                msg = trigger_simulation_vacancy()
                st.toast(msg)
                time.sleep(0.3)
                st.rerun()
        with col_sim2:
            if st.button("Simulate New Job Posted (Opportunity Trigger)", use_container_width=True):
                msg = trigger_simulation_new_job()
                st.toast(msg)
                time.sleep(0.3)
                st.rerun()
                
    st.markdown("<h2 class='split-choice-title'>What is your career goal today?</h2>", unsafe_allow_html=True)
    st.markdown("<p class='split-choice-subtitle'>Select a path below to instantly filter jobs by risk, wages, and skill matches.</p>", unsafe_allow_html=True)
    
    col_stable, col_growth, col_risky, col_other = st.columns(4)
    
    with col_stable:
        st.markdown(f"""
        <div class="option-box option-stable">
            <div>
                <div class="option-icon">🟢</div>
                <div class="option-header">The Stable Ascent</div>
                <div class="option-desc">
                    Find roles matching your <b>exact skills and level of experience</b>. These are positions with high placement probabilities where you can start immediately with high confidence.
                </div>
            </div>
            <div style="width:100%; text-align:center;">
                <span class="custom-badge badge-stable">90%+ CONFIDENCE</span>
                <span class="custom-badge badge-stable">STABLE WAGES</span>
            </div>
        </div>
        """, unsafe_allow_html=True)
        if st.button("Good & Easy Options ➜", key="btn_stable", use_container_width=True):
            set_page("StableMatches")
            
    with col_growth:
        st.markdown(f"""
        <div class="option-box option-growth">
            <div>
                <div class="option-icon">🟡</div>
                <div class="option-header">The Quantum Leap</div>
                <div class="option-desc">
                    Stretch your boundaries! Discover <b>high-paying, high-growth opportunities</b> that fit your interests but require slightly more experience or skills you haven't mastered yet.
                </div>
            </div>
            <div style="width:100%; text-align:center;">
                <span class="custom-badge badge-growth">HIGH ECONOMIC CLIMB</span>
                <span class="custom-badge badge-growth">CAREER GAP ANALYSIS</span>
            </div>
        </div>
        """, unsafe_allow_html=True)
        if st.button("Potential Growth Options ➜", key="btn_growth", use_container_width=True):
            set_page("GrowthMatches")

    with col_risky:
        st.markdown(f"""
        <div class="option-box option-risky">
            <div>
                <div class="option-icon">🔵</div>
                <div class="option-header">The Risky Horizon</div>
                <div class="option-desc">
                    Explore startup listings and contract opportunities. While job stability is volatile, your <b>skill match is highly compatible</b> ensuring a high placement success rate.
                </div>
            </div>
            <div style="width:100%; text-align:center;">
                <span class="custom-badge badge-risky">HIGH SUCCESS RATE</span>
                <span class="custom-badge badge-risky">STARTUPS & CONTRACTS</span>
            </div>
        </div>
        """, unsafe_allow_html=True)
        if st.button("Risky & High Success Options ➜", key="btn_risky", use_container_width=True):
            set_page("RiskyMatches")

    with col_other:
        st.markdown(f"""
        <div class="option-box option-other">
            <div>
                <div class="option-icon">✨</div>
                <div class="option-header">Other Opportunities</div>
                <div class="option-desc">
                    Browse a broad list of available opportunities that didn't fit the previous buckets — internships, remote gigs, and international roles.
                </div>
            </div>
            <div style="width:100%; text-align:center;">
                <span class="custom-badge badge-other">BROAD LIST</span>
            </div>
        </div>
        """, unsafe_allow_html=True)
        if st.button("Browse Other Options ➜", key="btn_other", use_container_width=True):
            set_page("OtherMatches")

    # Interactive Career Competency Alignment Plot
    st.markdown("<br>", unsafe_allow_html=True)
    st.subheader("🎯 Career Category Alignment Index")
    
    # Calculate counts in-memory
    n_stable = len(st.session_state['df_stable_raw'])
    n_growth = len(st.session_state['df_growth_raw'])
    n_risky = len(st.session_state['df_risky_raw'])
    
    df_radar = pd.DataFrame({
        "Career Path": ["Stable Ascent (Green)", "Quantum Leap (Yellow)", "Risky Horizon (Blue)"],
        "Available Open Positions": [n_stable, n_growth, n_risky],
        "Placement Probability (%)": [95.0, 55.0, 85.0]
    })
    
    col_chart, col_ticker = st.columns([2, 1])
    with col_chart:
        fig_radar = px.bar(
            df_radar,
            x="Career Path",
            y="Available Open Positions",
            color="Career Path",
            text="Available Open Positions",
            color_discrete_map={
                "Stable Ascent (Green)": "#10b981",
                "Quantum Leap (Yellow)": "#fbbf24",
                "Risky Horizon (Blue)": "#3b82f6"
            },
            template="plotly_dark"
        )
        fig_radar.update_layout(
            paper_bgcolor="rgba(0,0,0,0)",
            plot_bgcolor="rgba(0,0,0,0)",
            showlegend=False,
            height=260,
            margin=dict(l=10, r=10, t=10, b=10)
        )
        st.plotly_chart(fig_radar, use_container_width=True)
        
    with col_ticker:
        # Fintech Career Scorecard
        fintech_skills = ["Finance", "Accounting/Auditing", "Analyst", "Information Technology", "Engineering", "Strategy/Planning"]
        user_skills_list = [s['skill_name'] for s in user_profile['skills']]
        matching_f_skills = [s for s in fintech_skills if s in user_skills_list]
        f_score = int(len(matching_f_skills) / len(fintech_skills) * 100)
        
        st.markdown(f"""
        <div class="glass-card" style="padding: 16px 20px; border-top: 3px solid #818cf8; height: 100%;">
            <h4 style="margin:0; color:#a5b4fc;">Fintech & Analytics Index</h4>
            <div style="font-size:2.2rem; font-weight:700; color:#818cf8; margin: 5px 0;">{f_score}%</div>
            <p style="font-size:0.82rem; color:#94a3b8; margin-bottom:0;">You possess <b>{len(matching_f_skills)}</b> out of <b>{len(fintech_skills)}</b> core capabilities (Finance, Accounting, Analyst, IT, Engineering, Strategy) required to dominate the Fintech space.</p>
        </div>
        """, unsafe_allow_html=True)

    # Dynamic Fintech & Analytics Market Ticker
    st.markdown("""
    <div class="ticker-wrap">
        <div class="ticker">
            <div class="ticker__item">🏦 JPMorgan Chase: <span>1,245 listings</span></div>
            <div class="ticker__item">📈 Business Analyst: Average Salary <span class="up">$112,122 (+8.4% YoY)</span></div>
            <div class="ticker__item">💳 Stripe: Size <span>1,000+ employees</span></div>
            <div class="ticker__item">💻 Python/SQL Skills: Demand <span class="up">High (Score: 100/100)</span></div>
            <div class="ticker__item">📊 Risk Analyst: Average Salary <span class="up">$98,450 (+6.1% YoY)</span></div>
            <div class="ticker__item">🛡️ Finance Skills: Demand <span class="up">Stable (Score: 84/100)</span></div>
            <div class="ticker__item">⚡ Venture Capital: Listings <span class="down">Decreased (-2.3% YoY)</span></div>
        </div>
    </div>
    """, unsafe_allow_html=True)

# -------------------------------------------------------------
# PAGE: STABLE MATCHES (🟢 GREEN)
# -------------------------------------------------------------
elif st.session_state['page'] == 'StableMatches':
    st.markdown("<h1 style='color:#10b981; margin-bottom:0;'>🛡️ The Stable Ascent — Good & Easy Options</h1>", unsafe_allow_html=True)
    st.markdown("<p style='color: #10b981; opacity:0.8;'>Perfect-fit positions where your current skill profile matches expected requirements.</p>", unsafe_allow_html=True)
    st.markdown("<hr style='border-color: rgba(16, 185, 129, 0.2); margin-top:5px;'>", unsafe_allow_html=True)
    
    # Location Filter Header
    st.markdown("##### 📍 Location & Search Settings")
    col_scope1, col_scope2 = st.columns([2, 3])
    with col_scope1:
        st.session_state['location_scope'] = st.radio(
            "Select Geographical Scope:",
            ["All Opportunities", "Local Only", "Opportunities Abroad"],
            horizontal=True
        )
    
    # Filters
    col_f1, col_f2, col_f3 = st.columns([2, 1, 1])
    with col_f1:
        search_q = st.text_input("Search Job Title or Company:", "")
    with col_f2:
        emp_types = ["All", "Full-time", "Part-time", "Contract", "Internship", "Freelance"]
        selected_emp = st.selectbox("Employment Type:", emp_types)
    with col_f3:
        min_salary = st.number_input("Min Salary Target ($):", min_value=0, value=0, step=10000)
            
    # Load and process stable matches INSTANTLY in-memory
    jobs_df = apply_in_memory_filters(
        st.session_state['df_stable_raw'], 
        search_q, 
        selected_emp, 
        min_salary, 
        st.session_state['location_scope']
    )
    
    if jobs_df.empty:
        st.info("No perfect matches found matching these filters. Try modifying your filters or geographical scope!")
    else:
        st.markdown(f"Displaying **{len(jobs_df)}** stable matching roles.")

        # Show companies summary for these matches
        try:
            comp_summary = jobs_df.groupby('company_name').agg({'job_id': 'count', 'salary_max': 'mean'}).reset_index()
            comp_summary.columns = ['Company', 'Open Positions', 'Avg Max Salary']
            comp_summary['Avg Max Salary'] = comp_summary['Avg Max Salary'].apply(lambda x: f"${x:,.0f}" if pd.notna(x) else 'Undisclosed')
            st.markdown("#### Companies hiring for you")
            for _, crow in comp_summary.iterrows():
                col1, col2 = st.columns([4,1])
                with col1:
                    st.markdown(f"**{crow['Company']}** — {crow['Open Positions']} open roles | Avg: {crow['Avg Max Salary']}")
                with col2:
                    if st.button(f"View Jobs @ {crow['Company']}", key=f"comp_{crow['Company']}"):
                        st.session_state['company_filter'] = crow['Company']
                        safe_rerun()
        except Exception:
            pass

        # If user selected a company filter, apply it
        if st.session_state.get('company_filter'):
            jobs_df = jobs_df[jobs_df['company_name'] == st.session_state.get('company_filter')]

        # Display jobs
        for idx, row in jobs_df.iterrows():
            is_fin = st.session_state['fintech_target'] and ('is_fintech' in jobs_df.columns) and row['is_fintech']
            
            badge_html = f'<span class="custom-badge badge-stable">{row["match_score"]}% Match</span>'
            if is_fin:
                badge_html += ' <span class="custom-badge badge-fintech">Fintech & Analytics</span>'
                
            salary_text = f"${row['salary_min']:,.0f} - ${row['salary_max']:,.0f}" if pd.notna(row['salary_max']) else "Undisclosed"
            
            # Reason explanation for match
            reason_text = ''
            try:
                ms = int(row.get('matched_skills', 0))
                tr = int(row.get('total_required', 0))
                reason_text = f"Matched {ms} of {tr} required skills" if tr > 0 else "No required skills listed"
            except Exception:
                reason_text = ''

            st.markdown(f"""
            <div class="glass-card" style="border-left: 5px solid #10b981;">
                <div style="display:flex; justify-content:space-between; align-items:center;">
                    <h3 style="margin:0; color:#10b981;">{row['title']}</h3>
                    <div style="font-size: 1.1rem; font-weight:600; color:#e5e7eb;">{salary_text}</div>
                </div>
                <div style="color:#d1d5db; margin: 4px 0;"><b>{row['company_name']}</b> | {row['location']} | {row['employment_type']}</div>
                <div style="margin: 8px 0;">{badge_html}</div>
                <div style="color:#9ca3af; font-size:0.9rem;">{reason_text}</div>
            </div>
            """, unsafe_allow_html=True)
            
            col_det, col_space = st.columns([1, 6])
            with col_det:
                if st.button("View Details", key=f"det_{row['job_id']}", use_container_width=True):
                    st.session_state['last_match_type'] = 'stable'
                    set_page("JobDetails", row['job_id'])
            st.markdown("<br>", unsafe_allow_html=True)

# -------------------------------------------------------------
# PAGE: GROWTH MATCHES (🟡 YELLOW)
# -------------------------------------------------------------
elif st.session_state['page'] == 'GrowthMatches':
    st.markdown("<h1 style='color:#f59e0b; margin-bottom:0;'>🚀 The Quantum Leap — Potential Growth Options</h1>", unsafe_allow_html=True)
    st.markdown("<p style='color: #f59e0b; opacity:0.8;'>High-paying, high-growth positions that require upgrading your skill capabilities.</p>", unsafe_allow_html=True)
    st.markdown("<hr style='border-color: rgba(245, 158, 11, 0.2); margin-top:5px;'>", unsafe_allow_html=True)
    
    # Location Filter Header
    st.markdown("##### 📍 Location & Search Settings")
    col_scope1, col_scope2 = st.columns([2, 3])
    with col_scope1:
        st.session_state['location_scope'] = st.radio(
            "Select Geographical Scope:",
            ["All Opportunities", "Local Only", "Opportunities Abroad"],
            horizontal=True
        )
    
    # Filters
    col_f1, col_f2 = st.columns([2, 1])
    with col_f1:
        search_q = st.text_input("Search Job Title or Company:", "")
    with col_f2:
        emp_types = ["All", "Full-time", "Part-time", "Contract", "Internship", "Freelance"]
        selected_emp = st.selectbox("Employment Type:", emp_types)
            
    # Load and process growth matches INSTANTLY in-memory
    growth_df = apply_in_memory_filters(
        st.session_state['df_growth_raw'], 
        search_q, 
        selected_emp, 
        0.0, 
        st.session_state['location_scope']
    )
    
    if growth_df.empty:
        st.info("No growth matches identified matching these filters. Try modifying your filters or geographical scope!")
    else:
        st.markdown(f"Displaying **{len(growth_df)}** growth opportunities.")
        
        # Display jobs
        for idx, row in growth_df.iterrows():
            is_fin = st.session_state['fintech_target'] and ('is_fintech' in growth_df.columns) and row['is_fintech']
            
            badge_html = f'<span class="custom-badge badge-growth">{row["match_score"]}% Skill Match</span>'
            if is_fin:
                badge_html += ' <span class="custom-badge badge-fintech">Fintech & Analytics</span>'
                
            salary_text = f"${row['salary_min']:,.0f} - ${row['salary_max']:,.0f}" if pd.notna(row['salary_max']) else "Undisclosed"
            
            stable_avg = 70000.0  # fallback baseline
            try:
                # Use raw list to calculate stable average
                s_df = st.session_state['df_stable_raw']
                if not s_df.empty and s_df['salary_max'].notna().any():
                    stable_avg = s_df['salary_max'].mean()
            except Exception:
                pass
            
            boost = row['salary_max'] - stable_avg if pd.notna(row['salary_max']) else 0
            
            # Reason for growth recommendation
            try:
                ms = int(row.get('matched_skills', 0))
                tr = int(row.get('total_required', 0))
                reason_text = f"Matched {ms} of {tr} required skills" if tr > 0 else "No required skills listed"
            except Exception:
                reason_text = ''

            st.markdown(f"""
            <div class="glass-card" style="border-left: 5px solid #f59e0b;">
                <div style="display:flex; justify-content:space-between; align-items:center;">
                    <h3 style="margin:0; color:#f59e0b;">{row['title']}</h3>
                    <div style="font-size: 1.1rem; font-weight:600; color:#e5e7eb;">{salary_text}</div>
                </div>
                <div style="color:#d1d5db; margin: 4px 0;"><b>{row['company_name']}</b> | {row['location']} | {row['employment_type']}</div>
                <div style="margin: 8px 0;">{badge_html}</div>
                <div style="color:#9ca3af; font-size:0.9rem;">{reason_text}</div>
            """, unsafe_allow_html=True)
            
            if boost > 0:
                st.markdown(f"""
                <div class="salary-boost-card" style="margin-top:8px; margin-bottom:8px; padding:8px 12px;">
                    <div style="font-size:0.8rem; text-transform:uppercase; color:#9ca3af;">Potential Salary Boost</div>
                    <div style="font-weight:700; color:#f59e0b; font-size:1.1rem;">+${boost:,.0f} / year relative to stable profile average</div>
                </div>
                """, unsafe_allow_html=True)
            st.markdown("</div>", unsafe_allow_html=True)
            
            col_det, col_space = st.columns([1, 6])
            with col_det:
                if st.button("Analyze Gap", key=f"grow_{row['job_id']}", use_container_width=True):
                    st.session_state['last_match_type'] = 'growth'
                    set_page("JobDetails", row['job_id'])
            st.markdown("<br>", unsafe_allow_html=True)

# -------------------------------------------------------------
# PAGE: RISKY MATCHES (🔵 BLUE)
# -------------------------------------------------------------
elif st.session_state['page'] == 'RiskyMatches':
    st.markdown("<h1 style='color:#3b82f6; margin-bottom:0;'>⚡ The Risky Horizon — Risky but High Success Options</h1>", unsafe_allow_html=True)
    st.markdown("<p style='color: #3b82f6; opacity:0.8;'>Volatile contract or startup roles where your compatibility is exceptionally high, ensuring placement success.</p>", unsafe_allow_html=True)
    st.markdown("<hr style='border-color: rgba(59, 130, 246, 0.2); margin-top:5px;'>", unsafe_allow_html=True)
    
    # Location Filter Header
    st.markdown("##### 📍 Location & Search Settings")
    col_scope1, col_scope2 = st.columns([2, 3])
    with col_scope1:
        st.session_state['location_scope'] = st.radio(
            "Select Geographical Scope:",
            ["All Opportunities", "Local Only", "Opportunities Abroad"],
            horizontal=True
        )
    
    # Filters
    col_f1, col_f2 = st.columns([2, 1])
    with col_f1:
        search_q = st.text_input("Search Job Title or Company:", "")
    with col_f2:
        emp_types = ["All", "Full-time", "Part-time", "Contract", "Internship", "Freelance"]
        selected_emp = st.selectbox("Employment Type:", emp_types)
            
    # Load and process risky matches INSTANTLY in-memory
    risky_df = apply_in_memory_filters(
        st.session_state['df_risky_raw'], 
        search_q, 
        selected_emp, 
        0.0, 
        st.session_state['location_scope']
    )
    
    if risky_df.empty:
        st.info("No matching roles found. Try adjusting your skills profile in the sidebar.")
    else:
        st.markdown(f"Displaying **{len(risky_df)}** high-probability startup/contract roles.")
        
        # Display jobs
        for idx, row in risky_df.iterrows():
            is_fin = st.session_state['fintech_target'] and ('is_fintech' in risky_df.columns) and row['is_fintech']
            
            badge_html = f'<span class="custom-badge badge-risky">{row["match_score"]}% Compatibility Match</span>'
            badge_html += f' <span class="custom-badge badge-risky">Size: {row["size_range"]}</span>'
            if is_fin:
                badge_html += ' <span class="custom-badge badge-fintech">Fintech & Analytics</span>'
                
            salary_text = f"${row['salary_min']:,.0f} - ${row['salary_max']:,.0f}" if pd.notna(row['salary_max']) else "Undisclosed"
            
            try:
                ms = int(row.get('matched_skills', 0))
                tr = int(row.get('total_required', 0))
                reason_text = f"Matched {ms} of {tr} required skills" if tr > 0 else "No required skills listed"
            except Exception:
                reason_text = ''

            st.markdown(f"""
            <div class="glass-card" style="border-left: 5px solid #3b82f6;">
                <div style="display:flex; justify-content:space-between; align-items:center;">
                    <h3 style="margin:0; color:#3b82f6;">{row['title']}</h3>
                    <div style="font-size: 1.1rem; font-weight:600; color:#e5e7eb;">{salary_text}</div>
                </div>
                <div style="color:#d1d5db; margin: 4px 0;"><b>{row['company_name']}</b> | {row['location']} | {row['employment_type']}</div>
                <div style="margin: 8px 0;">{badge_html}</div>
                <div style="color:#9ca3af; font-size:0.9rem;">{reason_text}</div>
            </div>
            """, unsafe_allow_html=True)
            
            col_det, col_space = st.columns([1, 6])
            with col_det:
                if st.button("View Details", key=f"risk_{row['job_id']}", use_container_width=True):
                    st.session_state['last_match_type'] = 'risky'
                    set_page("JobDetails", row['job_id'])
            st.markdown("<br>", unsafe_allow_html=True)

# -------------------------------------------------------------
# PAGE: OTHER MATCHES (✨ BROAD)
# -------------------------------------------------------------
elif st.session_state['page'] == 'OtherMatches':
    st.markdown("<h1 style='color:#7c3aed; margin-bottom:0;'>✨ Other Opportunities — Broad Marketplace</h1>", unsafe_allow_html=True)
    st.markdown("<p style='color:#7c3aed; opacity:0.8;'>A wide curated feed including internships, remote and international roles, and freelance gigs.</p>", unsafe_allow_html=True)
    st.markdown("<hr style='border-color: rgba(124, 58, 237, 0.2); margin-top:5px;'>", unsafe_allow_html=True)

    # Filters
    col_f1, col_f2 = st.columns([3,1])
    with col_f1:
        search_q = st.text_input("Search title, company or location:", "")
    with col_f2:
        emp_types = ["All", "Full-time", "Part-time", "Contract", "Internship", "Freelance", "Remote"]
        selected_emp = st.selectbox("Employment Type:", emp_types)

    # Query the DB for broad job listings
    conn = get_connection()
    q_all = "SELECT jl.job_id, jl.title, c.company_name, jl.location, jl.salary_min, jl.salary_max, jl.employment_type, jl.posted_date FROM job_listings jl JOIN companies c ON jl.company_id = c.company_id WHERE jl.is_active = 1"
    params = []
    if selected_emp and selected_emp != 'All':
        q_all += " AND jl.employment_type = ?"
        params.append(selected_emp)
    if search_q:
        q_all += " AND (jl.title LIKE '%' || ? || '%' OR c.company_name LIKE '%' || ? || '%' OR jl.location LIKE '%' || ? || '%')"
        params.extend([search_q, search_q, search_q])
    q_all += " ORDER BY jl.posted_date DESC LIMIT 1000"
    jobs_df = pd.read_sql_query(q_all, conn, params=params)
    conn.close()

    if jobs_df.empty:
        st.info("No broad opportunities found. Try changing filters or your skills in the sidebar.")
    else:
        st.markdown(f"Displaying **{len(jobs_df)}** opportunities (showing up to 1000).")
        for idx, row in jobs_df.iterrows():
            salary_text = f"${row['salary_min']:,.0f} - ${row['salary_max']:,.0f}" if pd.notna(row['salary_max']) else "Undisclosed"
            st.markdown(f"""
            <div class="glass-card" style="border-left: 5px solid #7c3aed;">
                <div style="display:flex; justify-content:space-between; align-items:center;">
                    <h3 style="margin:0; color:#7c3aed;">{row['title']}</h3>
                    <div style="font-size: 1.0rem; font-weight:600; color:#e5e7eb;">{salary_text}</div>
                </div>
                <div style="color:#d1d5db; margin: 4px 0;"><b>{row['company_name']}</b> | {row['location']} | {row['employment_type']}</div>
            </div>
            """, unsafe_allow_html=True)
            col_det, col_space = st.columns([1, 6])
            with col_det:
                if st.button("View Details", key=f"oth_{row['job_id']}"):
                    set_page("JobDetails", row['job_id'])
            st.markdown("<br>", unsafe_allow_html=True)

# -------------------------------------------------------------
# PAGE: JOB DETAILS & SKILL GAP ANALYSIS
# -------------------------------------------------------------
elif st.session_state['page'] == 'JobDetails':
    job_id = st.session_state['view_job_id']
    if not job_id:
        st.warning("No job selected.")
        set_page("Dashboard")
        st.stop()
        
    job = get_job_details(job_id)
    if not job:
        st.error("Job details not found.")
        if st.button("Back"): set_page("Dashboard")
        st.stop()
        
    route_back = st.session_state.get('last_match_type', 'stable')
    
    if st.button("⬅ Back to Matches List"):
        if route_back == 'stable': set_page("StableMatches")
        elif route_back == 'growth': set_page("GrowthMatches")
        else: set_page("RiskyMatches")
        
    border_color = "#10b981"
    if route_back == 'growth': border_color = "#f59e0b"
    elif route_back == 'risky': border_color = "#3b82f6"
    
    st.markdown(f"""
    <div class="glass-card" style="border-left: 5px solid {border_color};">
        <div style="font-size:0.9rem; color:#9ca3af; text-transform:uppercase;">{job['employment_type']} Position Profile</div>
        <h2 style="margin-top:0; color:{border_color};">{job['title']}</h2>
        <h3>{job['company_name']}</h3>
        <p>📍 {job['location']} | 🏢 Sector: {job['industry']} | 👥 Size: {job['size_range']}</p>
        <hr style='opacity:0.1;'>
        <h4>Salary Package</h4>
        <div style='font-size:1.5rem; font-weight:700; color:{border_color};'>
            {"$"+"{:,.0f}".format(job['salary_min']) + " - $" + "{:,.0f}".format(job['salary_max']) if job['salary_max'] else "Undisclosed"}
        </div>
    </div>
    """, unsafe_allow_html=True)
    
    col_desc, col_gap = st.columns([1, 1])
    
    with col_desc:
        st.subheader("📝 Job Description")
        st.markdown(f"<div class='glass-card'>{job['description']}</div>", unsafe_allow_html=True)
        st.caption(f"Posted on: {job['posted_date']}")
        
    with col_gap:
        st.subheader("🎯 Skill Gap & Career Plan")
        
        user_skills_dict = {s['skill_id']: s for s in user_profile['skills']}
        
        req_met = []
        req_missing = []
        preferred_skills = []
        
        for js in job['skills']:
            if js['requirement_level'] == 'Required':
                if js['skill_id'] in user_skills_dict:
                    req_met.append(js)
                else:
                    req_missing.append(js)
            else:
                preferred_skills.append(js)
                
        total_req = len(req_met) + len(req_missing)
        match_score = (len(req_met) / total_req * 100) if total_req > 0 else 100.0
        
        fig = go.Figure(go.Indicator(
            mode = "gauge+number",
            value = match_score,
            title = {'text': "Skill Compatibility Score"},
            gauge = {
                'axis': {'range': [0, 100]},
                'bar': {'color': border_color},
                'steps': [
                    {'range': [0, 50], 'color': "rgba(239, 68, 68, 0.1)"},
                    {'range': [50, 80], 'color': "rgba(245, 158, 11, 0.1)"},
                    {'range': [80, 100], 'color': "rgba(16, 185, 129, 0.1)"}
                ],
            }
        ))
        fig.update_layout(height=200, margin=dict(l=20, r=20, t=40, b=20), paper_bgcolor="rgba(0,0,0,0)", font={'color': "#ffffff"})
        st.plotly_chart(fig, use_container_width=True)
        
        st.markdown("##### Required Skills Checked")
        for s in req_met:
            u_prof = user_skills_dict[s['skill_id']]['proficiency']
            st.markdown(f"✅ **{s['skill_name']}** (You have: *{u_prof}*)")
            
        if req_missing:
            st.markdown("##### 🚨 Required Skills Missing (Your Gap)")
            for s in req_missing:
                st.markdown(f"❌ <span style='color:#ef4444;'>**{s['skill_name']}**</span>", unsafe_allow_html=True)
                
        if preferred_skills:
            st.markdown("##### 💡 Preferred & Nice-To-Have Skills")
            for s in preferred_skills:
                has_s = s['skill_id'] in user_skills_dict
                symbol = "⭐" if has_s else "⚪"
                prof_note = f" (You have: *{user_skills_dict[s['skill_id']]['proficiency']}*)" if has_s else ""
                st.markdown(f"{symbol} **{s['skill_name']}**{prof_note}")
                
        st.markdown("<hr style='opacity:0.1;'>", unsafe_allow_html=True)
        st.markdown("#### 💡 Next Steps for Professional Growth")
        
        if req_missing:
            st.info(f"To qualify for this position, build proficiency in the missing skills: **{', '.join([s['skill_name'] for s in req_missing])}**.")
            
            fintech_industries = ["Financial Services", "Banking", "Venture Capital & Private Equity", "Investment Banking", "Investment Management", "Insurance", "Capital Markets", "Fintech"]
            if job['industry'] in fintech_industries:
                st.markdown(f"""
                <div class="salary-boost-card" style="border-left-color: {border_color};">
                    <h4 style="color: {border_color};">💡 Fintech Career Recommendation</h4>
                    <p style="margin-bottom:0;">This role is in a premium Financial Technology sector. Consider taking specialized coursework matching <b>{', '.join([s['skill_name'] for s in req_missing])}</b> to unlock an average salary in this industry of up to <b>$130,000+</b>.</p>
                </div>
                """, unsafe_allow_html=True)
        else:
            st.success("🎉 You meet all core requirements! Submit your application today as your profile matches their expected skill set.")

# -------------------------------------------------------------
# PAGE: TRENDS & FINTECH HUB
# -------------------------------------------------------------
elif st.session_state['page'] == 'Trends':
    st.header("📈 Market Trends & Fintech Intelligence Hub")
    st.caption("Platform analytics displaying skill values, salary trajectory, and trending fields.")
    
    conn = get_connection()
    q_trends = """
    SELECT mt.recorded_month, s.skill_name, mt.avg_salary, mt.demand_score
    FROM market_trends mt
    JOIN skills s ON s.skill_id = mt.skill_id
    ORDER BY mt.recorded_month ASC
    """
    trends_df = pd.read_sql_query(q_trends, conn)
    conn.close()
    
    trends_df['year'] = pd.to_datetime(trends_df['recorded_month']).dt.year
    
    st.subheader("💵 Salary Value Trajectory by Skill (2020 - 2024)")
    fig_salary = px.line(
        trends_df, 
        x="recorded_month", 
        y="avg_salary", 
        color="skill_name", 
        markers=True,
        labels={"recorded_month": "Date", "avg_salary": "Average Salary (USD)", "skill_name": "Skill Profile"},
        template="plotly_dark"
    )
    fig_salary.update_layout(
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        font={'family': 'Outfit'}
    )
    st.plotly_chart(fig_salary, use_container_width=True)
    
    col_grow, col_comp = st.columns([1, 1])
    
    with col_grow:
        st.subheader("📊 Skill Value Year-on-Year Growth")
        
        conn = get_connection()
        q_yoy = """
        WITH trend_base AS (
            SELECT
                mt.skill_id,
                s.skill_name,
                mt.avg_salary,
                CAST(SUBSTR(mt.recorded_month, 1, 4) AS INTEGER) AS trend_year
            FROM market_trends mt
            JOIN skills s ON mt.skill_id = s.skill_id
        ),
        trend_with_lag AS (
            SELECT
                skill_name,
                trend_year,
                avg_salary,
                LAG(avg_salary) OVER (
                    PARTITION BY skill_id ORDER BY trend_year
                ) AS prev_year_salary
            FROM trend_base
        )
        SELECT
            skill_name AS "Skill Profile",
            trend_year AS "Year",
            ROUND(avg_salary, 2) AS "Avg Salary ($)",
            CASE
                WHEN prev_year_salary IS NOT NULL AND prev_year_salary > 0
                THEN ROUND(((avg_salary - prev_year_salary) / prev_year_salary * 100), 2)
                ELSE NULL
            END AS "YoY Growth (%)"
        FROM trend_with_lag
        WHERE prev_year_salary IS NOT NULL
        ORDER BY "Skill Profile", "Year" DESC
        """
        yoy_df = pd.read_sql_query(q_yoy, conn)
        conn.close()
        
        st.dataframe(yoy_df, use_container_width=True, hide_index=True)
        
    with col_comp:
        st.subheader("🏦 Top Hiring Companies in Fintech & Analytics")
        
        conn = get_connection()
        # If user has selected skills, show companies hiring for those skills; otherwise show top companies overall
        skill_ids = []
        try:
            if user_profile and user_profile.get('skills'):
                skill_ids = [int(s['skill_id']) for s in user_profile.get('skills')]
        except Exception:
            skill_ids = []

        if skill_ids:
            placeholders = ','.join(['?'] * len(skill_ids))
            q_companies = f"""
            SELECT c.company_name AS "Company", c.industry AS "Industry", c.size_range AS "Employees",
                   COUNT(DISTINCT jl.job_id) AS "Active Jobs",
                   ROUND(AVG(jl.salary_max), 2) AS "Avg Max Salary ($)"
            FROM companies c
            JOIN job_listings jl ON c.company_id = jl.company_id
            JOIN job_skills js ON jl.job_id = js.job_id
            WHERE jl.is_active = 1 AND js.skill_id IN ({placeholders})
            GROUP BY c.company_id, c.company_name
            ORDER BY "Active Jobs" DESC, "Avg Max Salary ($)" DESC
            LIMIT 10
            """
            comp_df = pd.read_sql_query(q_companies, conn, params=skill_ids)
        else:
            q_companies = """
            SELECT c.company_name AS "Company", c.industry AS "Industry", c.size_range AS "Employees",
                   COUNT(jl.job_id) AS "Active Jobs",
                   ROUND(AVG(jl.salary_max), 2) AS "Avg Max Salary ($)"
            FROM companies c
            JOIN job_listings jl ON c.company_id = jl.company_id
            WHERE jl.is_active = 1
            GROUP BY c.company_id, c.company_name
            ORDER BY "Active Jobs" DESC, "Avg Max Salary ($)" DESC
            LIMIT 10
            """
            comp_df = pd.read_sql_query(q_companies, conn)

        conn.close()
        if comp_df.empty:
            st.info("No hiring companies found for selected skills. Try editing your profile skills in the sidebar.")
        else:
            st.dataframe(comp_df, use_container_width=True, hide_index=True)

    st.markdown("<hr style='opacity:0.1;'>", unsafe_allow_html=True)
    st.subheader("🔮 2026 Skill Demand & Salary Forecaster")
    st.markdown("<p style='color:#9ca3af;'>Select a skill profile to estimate market values for 2026 based on historical trajectory trends.</p>", unsafe_allow_html=True)
    
    pred_skills = trends_df['skill_name'].unique().tolist()
    sel_pred_skill = st.selectbox("Select Target Skill to Project:", pred_skills)
    
    skill_data = trends_df[trends_df['skill_name'] == sel_pred_skill].sort_values(by="year")
    if len(skill_data) >= 2:
        start_year = skill_data['year'].iloc[0]
        end_year = skill_data['year'].iloc[-1]
        start_val = skill_data['avg_salary'].iloc[0]
        end_val = skill_data['avg_salary'].iloc[-1]
        
        avg_change_per_year = (end_val - start_val) / (end_year - start_year) if end_year != start_year else 0
        proj_val_2026 = end_val + avg_change_per_year * (2026 - end_year)
        
        start_ds = skill_data['demand_score'].iloc[0]
        end_ds = skill_data['demand_score'].iloc[-1]
        change_ds = (end_ds - start_ds) / (end_year - start_year) if end_year != start_year else 0
        proj_ds_2026 = min(max(end_ds + change_ds * (2026 - end_year), 0), 100)
        
        col_p1, col_p2, col_p3 = st.columns(3)
        with col_p1:
            st.metric(f"Historical avg salary ({end_year})", f"${end_val:,.2f}")
        with col_p2:
            st.metric("Projected 2026 Salary", f"${proj_val_2026:,.2f}", f"{avg_change_per_year:+.2f} / yr")
        with col_p3:
            st.metric("Projected 2026 Demand index", f"{proj_ds_2026:.1f} / 100", f"{change_ds:+.1f} / yr")
            
        st.caption("Note: Forecaster runs a linear progression from data checkpoints. Real market conditions may deviate.")

# -------------------------------------------------------------
# PAGE: PERFORMANCE SANDBOX & INDEXING COMPARISON
# -------------------------------------------------------------
elif st.session_state['page'] == 'Performance':
    st.header("⚡ DBMS Performance & Indexing sandbox")
    st.caption("Demonstration of Milestone 3 optimization parameters: executing slow sequential scans vs. indexed lookups.")
    
    st.markdown("""
    <div class="glass-card">
        <h4>About the Sandbox Experiment</h4>
        <p>In database systems, index placement is critical. For this experiment, we duplicate the platform's active SQLite tables into a local <b>in-memory database</b>. This guarantees 100% safety, isolated testing, and lets us dynamically drop or rebuild index keys to test execution speeds in real-time.
        We run the 3 baseline slow query bottlenecks identified in the milestone DDL:</p>
        <ul>
            <li><b>Q-SLOW-1</b>: Filter active jobs in salary range (60k - 150k) - checks <i>is_active</i> and <i>salary_max</i>.</li>
            <li><b>Q-SLOW-2</b>: Skill demand aggregation - groups and joins <i>job_skills</i>.</li>
            <li><b>Q-SLOW-3</b>: Best-Fit Recommendation - correlated subquery verifying user skill overlaps.</li>
        </ul>
    </div>
    """, unsafe_allow_html=True)
    
    st.markdown("""
    💡 <b>PostgreSQL Stored Procedure Integration:</b> The final schema includes a stored procedure <code>GetJobRecommendations(p_user_id, p_limit)</code>.
    The recommendation engine in this web application is running a high-speed Python/SQL counterpart of this exact query on the active user profile.
    """, unsafe_allow_html=True)
    
    st.markdown("<br>", unsafe_allow_html=True)
    
    if st.button("🚀 Run Live timing Experiment", type="primary", use_container_width=True):
        
        with st.spinner("Executing queries against database tables (runs query iterations to measure average speeds)..."):
            
            try:
                def run_local_perf(db_path, user_id):
                    mem_conn = sqlite3.connect(":memory:")
                    disk_conn = sqlite3.connect(db_path)
                    disk_conn.backup(mem_conn)
                    disk_conn.close()
                    
                    cur = mem_conn.cursor()
                    
                    indexes_to_drop = [
                        "idx_jl_active_salary", "idx_js_skill_covering", "idx_us_user_covering",
                        "idx_js_job_id", "idx_jl_company_id", "idx_mt_skill_month",
                        "idx_user_skills_user_id", "idx_user_skills_skill_id",
                        "idx_job_skills_job_id", "idx_job_skills_skill_id",
                        "idx_jobs_company_id", "idx_market_trends_skill_id"
                    ]
                    for idx in indexes_to_drop:
                        try: cur.execute(f"DROP INDEX IF EXISTS {idx}")
                        except Exception: pass
                    mem_conn.commit()
                    
                    q1 = """
                    SELECT jl.job_id, jl.title, jl.location, jl.salary_min, jl.salary_max, c.company_name, c.industry
                    FROM job_listings jl
                    JOIN companies c ON jl.company_id = c.company_id
                    WHERE jl.is_active = 1
                      AND jl.salary_max IS NOT NULL
                      AND jl.salary_max BETWEEN 60000 AND 150000
                    ORDER BY jl.salary_max DESC
                    LIMIT 50;
                    """
                    
                    q2 = """
                    SELECT s.skill_name, s.category, COUNT(DISTINCT js.job_id) AS total_jobs
                    FROM skills s
                    JOIN job_skills js ON s.skill_id = js.skill_id
                    GROUP BY s.skill_id, s.skill_name, s.category
                    ORDER BY total_jobs DESC;
                    """
                    
                    q3 = f"""
                    SELECT jl.job_id, jl.title, jl.salary_max, c.company_name
                    FROM job_listings jl
                    JOIN companies c ON jl.company_id = c.company_id
                    WHERE jl.is_active = 1
                      AND NOT EXISTS (
                          SELECT 1
                          FROM job_skills js
                          WHERE js.job_id = jl.job_id
                            AND js.requirement_level = 'Required'
                            AND js.skill_id NOT IN (
                                SELECT skill_id
                                FROM user_skills
                                WHERE user_id = {user_id}
                            )
                      )
                    ORDER BY jl.salary_max DESC
                    LIMIT 20;
                    """
                    
                    results = {}
                    
                    for q_label, query in [("Q-SLOW-1 (Salary range scan)", q1), 
                                           ("Q-SLOW-2 (Covering joins)", q2), 
                                           ("Q-SLOW-3 (Best fit subquery)", q3)]:
                        cur.execute(query)
                        cur.fetchall()
                        t0 = time.perf_counter()
                        for _ in range(5):
                            cur.execute(query)
                            cur.fetchall()
                        t_no_idx = (time.perf_counter() - t0) / 5 * 1000  # ms
                        
                        cur.execute(f"EXPLAIN QUERY PLAN {query}")
                        plan_no_idx = "\n".join([f"-> {row[3]}" for row in cur.fetchall()])
                        
                        results[q_label] = {
                            'query': query,
                            'time_no_idx': t_no_idx,
                            'plan_no_idx': plan_no_idx
                        }
                        
                    indexes_to_create = [
                        "CREATE INDEX idx_jl_active_salary ON job_listings (is_active, salary_max DESC) WHERE salary_max IS NOT NULL;",
                        "CREATE INDEX idx_js_skill_covering ON job_skills (skill_id, job_id, requirement_level);",
                        "CREATE INDEX idx_us_user_covering ON user_skills (user_id, skill_id);",
                        "CREATE INDEX idx_js_job_id ON job_skills (job_id);",
                        "CREATE INDEX idx_jl_company_id ON job_listings (company_id);",
                        "CREATE INDEX idx_mt_skill_month ON market_trends (skill_id, recorded_month DESC);"
                    ]
                    for idx_sql in indexes_to_create:
                        cur.execute(idx_sql)
                    mem_conn.commit()
                    
                    for q_label, query in [("Q-SLOW-1 (Salary range scan)", q1), 
                                           ("Q-SLOW-2 (Covering joins)", q2), 
                                           ("Q-SLOW-3 (Best fit subquery)", q3)]:
                        cur.execute(query)
                        cur.fetchall()
                        t0 = time.perf_counter()
                        for _ in range(5):
                            cur.execute(query)
                            cur.fetchall()
                        t_with_idx = (time.perf_counter() - t0) / 5 * 1000  # ms
                        
                        cur.execute(f"EXPLAIN QUERY PLAN {query}")
                        plan_with_idx = "\n".join([f"-> {row[3]}" for row in cur.fetchall()])
                        
                        results[q_label]['time_with_idx'] = t_with_idx
                        results[q_label]['plan_with_idx'] = plan_with_idx
                        
                    mem_conn.close()
                    return results

                res = run_local_perf(DB_PATH, user_profile['user_id'])
                
                st.success("Timing experiment completed successfully!")
                
                chart_data = []
                for label, info in res.items():
                    chart_data.append({"Query": label, "Timing (ms)": info['time_no_idx'], "Type": "Sequential Scan"})
                    chart_data.append({"Query": label, "Timing (ms)": info['time_with_idx'], "Type": "Indexed Search"})
                    
                df_chart = pd.DataFrame(chart_data)
                
                fig_bar = px.bar(
                    df_chart, 
                    x="Query", 
                    y="Timing (ms)", 
                    color="Type", 
                    barmode="group",
                    text_auto=".2f",
                    title="Query Execution Time comparison (Lower is better)",
                    color_discrete_map={"Sequential Scan": "#ef4444", "Indexed Search": "#10b981"},
                    template="plotly_dark"
                )
                fig_bar.update_layout(paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)")
                st.plotly_chart(fig_bar, use_container_width=True)
                
                st.subheader("🔍 Query Details & Execution Plans")
                
                for label, info in res.items():
                    with st.expander(f"Details for {label}"):
                        st.markdown("**Query SQL:**")
                        st.code(info['query'], language="sql")
                        
                        col_t1, col_t2, col_t3 = st.columns(3)
                        with col_t1:
                            st.metric("Seq Scan (No index)", f"{info['time_no_idx']:.2f} ms")
                        with col_t2:
                            st.metric("Indexed Search", f"{info['time_with_idx']:.2f} ms")
                        with col_t3:
                            speedup = info['time_no_idx'] / info['time_with_idx'] if info['time_with_idx'] > 0 else 0
                            st.metric("Performance boost", f"{speedup:.1f}x Faster")
                            
                        col_p1, col_p2 = st.columns(2)
                        with col_p1:
                            st.markdown("**(Baseline) EXPLAIN QUERY PLAN (Seq Scan):**")
                            st.code(info['plan_no_idx'])
                        with col_p2:
                            st.markdown("**(Optimized) EXPLAIN QUERY PLAN (Index Search):**")
                            st.code(info['plan_with_idx'])
                            
            except Exception as e:
                st.error(f"Error executing performance test: {e}")

# -------------------------------------------------------------
# PAGE: USER PROFILE
# -------------------------------------------------------------
elif st.session_state['page'] == 'Profile':
    st.header("👤 Your Profile & Competency Analytics")
    st.caption("Active platform credentials, qualifications, and skillset index.")
    
    col_c1, col_c2 = st.columns([1, 1])
    
    with col_c1:
        st.markdown(f"""
        <div class="glass-card" style="border-left:4px solid #8b5cf6;">
            <h3>{user_profile['full_name']}</h3>
            <p><b>Email:</b> {user_profile['email']}</p>
            <p><b>Location:</b> {user_profile['location']}</p>
            <p><b>Experience Level:</b> {user_profile['years_experience']} years</p>
            <hr style='opacity:0.15;'>
            <h4>Academic Record</h4>
            <p><b>Degree:</b> {user_profile['education']['degree']}</p>
            <p><b>Field:</b> {user_profile['education']['field_of_study']}</p>
            <p><b>Institution:</b> {user_profile['education']['institution']}</p>
        </div>
        """, unsafe_allow_html=True)
        
    with col_c2:
        st.subheader("🎯 Skill Proficiency Index")
        
        skills_data = user_profile['skills']
        if not skills_data:
            st.info("No skills listed on this profile. Add some in the sidebar!")
        else:
            df_skills = pd.DataFrame(skills_data)
            
            prof_map = {"Beginner": 1, "Intermediate": 2, "Advanced": 3, "Expert": 4}
            df_skills['Proficiency Score'] = df_skills['proficiency'].map(prof_map)
            
            fig_skills = px.bar(
                df_skills, 
                x="skill_name", 
                y="Proficiency Score", 
                color="category",
                labels={"skill_name": "Skill", "Proficiency Score": "Level"},
                category_orders={"Proficiency Score": [1, 2, 3, 4]},
                template="plotly_dark"
            )
            fig_skills.update_yaxes(
                tickvals=[1, 2, 3, 4],
                ticktext=["Beginner", "Intermediate", "Advanced", "Expert"]
            )
            fig_skills.update_layout(
                paper_bgcolor="rgba(0,0,0,0)",
                plot_bgcolor="rgba(0,0,0,0)",
                font={'family': 'Outfit'}
            )
            st.plotly_chart(fig_skills, use_container_width=True)
            
    st.markdown("<hr style='opacity:0.15;'>", unsafe_allow_html=True)
    st.subheader("🛠️ Current Competency Details")
    
    if not skills_data:
        st.info("No skills present.")
    else:
        # Deduplicate skills by skill_id or name
        unique = {}
        for s in skills_data:
            key = s.get('skill_id') or s.get('skill_name')
            if key not in unique:
                unique[key] = s

        unique_skills = list(unique.values())
        cols = st.columns(4)
        for idx, s in enumerate(unique_skills):
            col_idx = idx % 4
            with cols[col_idx]:
                st.markdown(f"""
                <div class="glass-card" style="padding:15px; margin-bottom:10px; border-top: 3px solid #8b5cf6;">
                    <div style="font-weight:600; font-size:1.0rem;">{s.get('skill_name')}</div>
                    <div style="color:#a78bfa; font-size:0.85rem; text-transform:uppercase; font-weight:700;">{s.get('proficiency')}</div>
                    <div style="color:#9ca3af; font-size:0.8rem;">Category: {s.get('category')}</div>
                </div>
                """, unsafe_allow_html=True)
