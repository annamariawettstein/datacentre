-- Phase 0 headline analysis. Run after `datacentre dedupe`.
-- Every figure is reported at both application and site level where it matters —
-- doing the dedupe yourself and showing both is the credibility signal.

\echo '===== 1. Applications vs distinct sites ====='
SELECT
    count(*)                        AS applications,
    count(DISTINCT site_id)         AS distinct_sites
FROM application WHERE is_datacentre;

-- Primary applications only: the substantive "build it" decisions.
-- One representative Full/Outline application per site (latest by start_date).
\echo '===== 2a. Full+Outline outcome — APPLICATION level ====='
SELECT app_state,
       count(*),
       round(100.0*count(*)/sum(count(*)) OVER (), 1) AS pct
FROM application
WHERE is_datacentre AND app_type IN ('Full','Outline')
GROUP BY app_state ORDER BY 2 DESC;

\echo '===== 2b. Full+Outline outcome — SITE level (one primary app per site) ====='
WITH primary_app AS (
    SELECT *, row_number() OVER (
               PARTITION BY site_id ORDER BY start_date DESC NULLS LAST) AS rn
    FROM application
    WHERE is_datacentre AND app_type IN ('Full','Outline')
)
SELECT app_state,
       count(*),
       round(100.0*count(*)/sum(count(*)) OVER (), 1) AS pct
FROM primary_app WHERE rn = 1
GROUP BY app_state ORDER BY 2 DESC;

\echo '===== 2c. Refusal & withdrawal rates (site level, Full+Outline) ====='
WITH primary_app AS (
    SELECT *, row_number() OVER (
               PARTITION BY site_id ORDER BY start_date DESC NULLS LAST) AS rn
    FROM application
    WHERE is_datacentre AND app_type IN ('Full','Outline')
), sites AS (SELECT app_state FROM primary_app WHERE rn = 1)
SELECT
    count(*) FILTER (WHERE app_state='Rejected')                          AS refused,
    count(*) FILTER (WHERE app_state='Withdrawn')                         AS withdrawn,
    count(*) FILTER (WHERE app_state IN ('Permitted','Conditions'))       AS approved,
    round(100.0*count(*) FILTER (WHERE app_state='Rejected')
          / NULLIF(count(*) FILTER (WHERE app_state IN
              ('Permitted','Conditions','Rejected')),0), 1)              AS refusal_pct,
    round(100.0*count(*) FILTER (WHERE app_state='Withdrawn')
          / NULLIF(count(*) FILTER (WHERE app_state IN
              ('Permitted','Conditions','Rejected','Withdrawn')),0), 1)  AS withdrawal_pct
FROM sites;

\echo '===== 3. Time-in-system & overrun vs statutory target (Full+Outline, decided) ====='
WITH d AS (
    SELECT
        decided_date - (other_fields->>'date_validated')::date            AS days_in_system,
        decided_date - (other_fields->>'target_decision_date')::date      AS overrun_days
    FROM application
    WHERE is_datacentre AND app_type IN ('Full','Outline')
      AND decided_date IS NOT NULL
)
SELECT
    count(*) FILTER (WHERE days_in_system IS NOT NULL)                     AS n_timed,
    round(percentile_cont(0.5) WITHIN GROUP (ORDER BY days_in_system))     AS median_days,
    round(percentile_cont(0.5) WITHIN GROUP (ORDER BY overrun_days))       AS median_overrun_days,
    round(100.0*count(*) FILTER (WHERE overrun_days > 0)
          / NULLIF(count(*) FILTER (WHERE overrun_days IS NOT NULL),0),1)  AS pct_over_target
FROM d;

\echo '===== 3b. Acceptance length over time — median days to decision, by submission-year cohort ====='
-- Cohort = year the application was validated (submitted). Approvals only
-- (Permitted + Conditions) so this is genuinely "acceptance" length, not refusal.
-- NB recent cohorts are truncated: slower schemes in 2024-26 are still undecided,
-- so the latest years are biased fast — read the trend, not the last point.
WITH a AS (
    SELECT (other_fields->>'date_validated')::date                        AS validated,
           decided_date,
           decided_date - (other_fields->>'date_validated')::date         AS days,
           decided_date - (other_fields->>'target_decision_date')::date   AS overrun
    FROM application
    WHERE is_datacentre AND app_type IN ('Full','Outline')
      AND app_state IN ('Permitted','Conditions')
      AND decided_date IS NOT NULL
      AND (other_fields->>'date_validated') IS NOT NULL
)
SELECT extract(year FROM validated)::int                                  AS cohort_yr,
       count(*)                                                           AS n_approved,
       round(percentile_cont(0.25) WITHIN GROUP (ORDER BY days))          AS p25_days,
       round(percentile_cont(0.50) WITHIN GROUP (ORDER BY days))          AS median_days,
       round(percentile_cont(0.75) WITHIN GROUP (ORDER BY days))          AS p75_days,
       round(100.0*count(*) FILTER (WHERE overrun > 0)
             / NULLIF(count(*) FILTER (WHERE overrun IS NOT NULL),0),0)    AS pct_over_target
FROM a
WHERE validated >= '2015-01-01'
GROUP BY 1 ORDER BY 1;

\echo '===== 4. Monthly application counts by start_date (last 24 months) ====='
SELECT to_char(date_trunc('month', start_date),'YYYY-MM') AS month, count(*)
FROM application
WHERE is_datacentre AND start_date >= (CURRENT_DATE - INTERVAL '24 months')
GROUP BY 1 ORDER BY 1;

\echo '===== 4b. Yearly applications & NEW sites (first application per site) ====='
WITH site_start AS (
    SELECT site_id, extract(year FROM min(start_date))::int AS yr
    FROM application WHERE is_datacentre AND start_date IS NOT NULL
    GROUP BY site_id
), apps AS (
    SELECT extract(year FROM start_date)::int AS yr, count(*) AS applications
    FROM application WHERE is_datacentre AND start_date IS NOT NULL GROUP BY 1
), newsites AS (
    SELECT yr, count(*) AS new_sites FROM site_start GROUP BY yr
)
SELECT a.yr, a.applications, coalesce(n.new_sites,0) AS new_sites
FROM apps a LEFT JOIN newsites n USING (yr) ORDER BY a.yr;

\echo '===== 4c. Lapsed consents — expired permissions classified by later site activity ====='
-- "Permission expired" overstates death. Classify each expired permission by what
-- happened next on the same site. Only "total silence" supports an abandonment claim
-- (and even that is an upper bound — a scheme can be built to its original consent
-- with no further planning applications).
WITH expired AS (
    SELECT name, site_id, start_date AS granted_start
    FROM application
    WHERE is_datacentre
      AND (other_fields->>'permission_expires_date') ~ '^[0-9]{4}-'
      AND (other_fields->>'permission_expires_date')::date < current_date
), classified AS (
    SELECT e.site_id,
      EXISTS (SELECT 1 FROM application a
              WHERE a.site_id = e.site_id AND a.name <> e.name
                AND a.start_date > e.granted_start
                AND a.app_state IN ('Permitted','Conditions'))  AS later_approval,
      EXISTS (SELECT 1 FROM application a
              WHERE a.site_id = e.site_id AND a.name <> e.name
                AND a.start_date > e.granted_start)             AS later_any
    FROM expired e
)
SELECT
    CASE WHEN later_approval THEN 'superseded / implemented'
         WHEN later_any      THEN 're-applied / still active'
         ELSE 'expired then total silence (upper-bound dead)' END AS bucket,
    count(*)                AS expired_apps,
    count(DISTINCT site_id) AS distinct_sites
FROM classified GROUP BY 1 ORDER BY 3 DESC;

\echo '===== 5. Live pipeline — undecided sites for hand review ====='
SELECT count(DISTINCT site_id) AS undecided_sites,
       count(*)                AS undecided_applications
FROM application WHERE is_datacentre AND app_state = 'Undecided';
