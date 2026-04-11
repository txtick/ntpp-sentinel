from typing import Any


def ensure_dashboard_schema_definitions(conn: Any, *, monthly_chemical_cost_review_threshold: float) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS alert_rule_config (
                rule_code TEXT PRIMARY KEY,
                reading_key TEXT NOT NULL,
                comparator TEXT NOT NULL,
                severity TEXT NOT NULL,
                severity_rank INTEGER NOT NULL,
                threshold_value NUMERIC NOT NULL,
                enabled BOOLEAN NOT NULL DEFAULT TRUE,
                season_start_month SMALLINT,
                season_end_month SMALLINT,
                description TEXT,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
            """
        )

        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS trend_rule_config (
                rule_code TEXT PRIMARY KEY,
                reading_key TEXT NOT NULL,
                trend_type TEXT NOT NULL,
                comparator TEXT,
                severity TEXT NOT NULL,
                severity_rank INTEGER NOT NULL,
                threshold_value NUMERIC,
                sample_size INTEGER,
                min_bad_count INTEGER,
                window_days INTEGER,
                delta_threshold NUMERIC,
                baseline_delta_threshold NUMERIC,
                enabled BOOLEAN NOT NULL DEFAULT TRUE,
                season_start_month SMALLINT,
                season_end_month SMALLINT,
                description TEXT,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
            """
        )

        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS revenue_rule_config (
                rule_code TEXT PRIMARY KEY,
                opportunity_type TEXT NOT NULL,
                source_type TEXT NOT NULL,
                reading_key TEXT,
                trend_type TEXT,
                comparator TEXT,
                severity TEXT NOT NULL,
                severity_rank INTEGER NOT NULL,
                threshold_value NUMERIC,
                repeat_count INTEGER,
                window_days INTEGER,
                enabled BOOLEAN NOT NULL DEFAULT TRUE,
                season_start_month SMALLINT,
                season_end_month SMALLINT,
                description TEXT,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
            """
        )

        cur.execute(
            """
            CREATE OR REPLACE FUNCTION rule_applies_in_month(
                start_month SMALLINT,
                end_month SMALLINT,
                ref_date DATE DEFAULT CURRENT_DATE
            ) RETURNS BOOLEAN
            LANGUAGE SQL
            STABLE
            AS $$
                SELECT CASE
                    WHEN start_month IS NULL OR end_month IS NULL THEN TRUE
                    WHEN start_month <= end_month
                        THEN EXTRACT(MONTH FROM ref_date)::INT BETWEEN start_month AND end_month
                    ELSE EXTRACT(MONTH FROM ref_date)::INT >= start_month
                         OR EXTRACT(MONTH FROM ref_date)::INT <= end_month
                END
            $$;
            """
        )

        cur.execute(
            """
            CREATE OR REPLACE FUNCTION normalize_metric_key(input_text TEXT)
            RETURNS TEXT
            LANGUAGE SQL
            IMMUTABLE
            AS $$
                SELECT CASE
                    WHEN input_text IS NULL OR btrim(input_text) = '' THEN NULL
                    WHEN lower(input_text) LIKE '%free chlorine%' THEN 'free_chlorine'
                    WHEN lower(input_text) LIKE '%cyanuric acid%' OR lower(input_text) = 'cya' THEN 'cya'
                    WHEN lower(input_text) LIKE '%phosphat%' THEN 'phosphates'
                    WHEN lower(input_text) = 'ph' OR lower(input_text) LIKE 'ph %' OR lower(input_text) LIKE '% ph%' THEN 'ph'
                    WHEN lower(input_text) LIKE '%total alkalinity%' OR lower(input_text) LIKE '%alkalinity%' THEN 'alkalinity'
                    WHEN lower(input_text) LIKE '%calcium hardness%' OR lower(input_text) LIKE '%total hardness%' OR lower(input_text) LIKE '%hardness%' THEN 'calcium_hardness'
                    WHEN lower(input_text) LIKE '%filter pressure%' OR lower(input_text) = 'psi' THEN 'filter_pressure'
                    WHEN lower(input_text) LIKE '%salt%' THEN 'salt'
                    WHEN lower(input_text) LIKE '%tds%' THEN 'tds'
                    WHEN lower(input_text) LIKE '%saturation index%' OR lower(input_text) LIKE '%lsi%' THEN 'lsi'
                    WHEN lower(input_text) LIKE '%water temp%' OR lower(input_text) LIKE '%temperature%' THEN 'temperature'
                    WHEN lower(input_text) LIKE '%total chlorine%' THEN 'total_chlorine'
                    ELSE trim(BOTH '_' FROM regexp_replace(lower(input_text), '[^a-z0-9]+', '_', 'g'))
                END
            $$;
            """
        )

        cur.executemany(
            """
            INSERT INTO alert_rule_config (
                rule_code,
                reading_key,
                comparator,
                severity,
                severity_rank,
                threshold_value,
                enabled,
                season_start_month,
                season_end_month,
                description
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (rule_code) DO UPDATE
            SET
                reading_key = EXCLUDED.reading_key,
                comparator = EXCLUDED.comparator,
                severity = EXCLUDED.severity,
                severity_rank = EXCLUDED.severity_rank,
                threshold_value = EXCLUDED.threshold_value,
                enabled = EXCLUDED.enabled,
                season_start_month = EXCLUDED.season_start_month,
                season_end_month = EXCLUDED.season_end_month,
                description = EXCLUDED.description
            """,
            [
                ("cya_above_80", "cya", "gt", "critical", 20, 80, True, None, None, "CYA above 80 ppm"),
                ("phosphates_above_500", "phosphates", "gt", "warning", 10, 500, True, None, None, "Phosphates above 500 ppb"),
                ("phosphates_above_1000", "phosphates", "gt", "critical", 20, 1000, True, None, None, "Phosphates above 1000 ppb"),
            ],
        )
        cur.executemany(
            """
            INSERT INTO trend_rule_config (
                rule_code,
                reading_key,
                trend_type,
                comparator,
                severity,
                severity_rank,
                threshold_value,
                sample_size,
                min_bad_count,
                window_days,
                delta_threshold,
                baseline_delta_threshold,
                enabled,
                season_start_month,
                season_end_month,
                description
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (rule_code) DO UPDATE
            SET
                reading_key = EXCLUDED.reading_key,
                trend_type = EXCLUDED.trend_type,
                comparator = EXCLUDED.comparator,
                severity = EXCLUDED.severity,
                severity_rank = EXCLUDED.severity_rank,
                threshold_value = EXCLUDED.threshold_value,
                sample_size = EXCLUDED.sample_size,
                min_bad_count = EXCLUDED.min_bad_count,
                window_days = EXCLUDED.window_days,
                delta_threshold = EXCLUDED.delta_threshold,
                baseline_delta_threshold = EXCLUDED.baseline_delta_threshold,
                enabled = EXCLUDED.enabled,
                season_start_month = EXCLUDED.season_start_month,
                season_end_month = EXCLUDED.season_end_month,
                description = EXCLUDED.description
            """,
            [
                ("fc_zero_2_of_2_14d", "free_chlorine", "bad_readings_last_n", "lte", "warning", 10, 0, 2, 2, 14, None, None, True, None, None, "2 free chlorine readings at 0 in the last 14 days"),
                ("fc_cya_ratio_bad_2wk", "fc_cya_ratio", "fc_cya_ratio_last_n", "lt", "warning", 10, 0.075, 2, 2, 21, None, None, True, None, None, "FC:CYA ratio below 7.5% on last 2 paired readings"),
                ("cya_rise_above_50_15_60d", "cya", "delta_over_days", None, "warning", 10, 50, None, None, 60, 15, None, True, None, None, "CYA above 50 and rising 15+ over 60 days"),
                ("phosphates_bad_3_of_5", "phosphates", "bad_readings_last_n", "gt", "warning", 10, 500, 5, 3, 60, None, None, True, None, None, "3 of last 5 phosphate readings above 500"),
                ("ph_bad_2_of_2_14d", "ph", "bad_readings_last_n", "gt", "warning", 10, 7.8, 2, 2, 14, None, None, True, None, None, "2 pH readings above 7.8 in the last 14 days"),
            ],
        )
        cur.execute(
            """
            DELETE FROM alert_rule_config
            WHERE rule_code IN ('fc_below_2', 'fc_below_1')
            """
        )
        cur.execute(
            """
            DELETE FROM trend_rule_config
            WHERE rule_code IN ('fc_bad_3_of_5')
            """
        )
        cur.execute(
            """
            DELETE FROM alert_rule_config
            WHERE rule_code IN ('cya_above_100')
            """
        )
        cur.execute(
            """
            DELETE FROM trend_rule_config
            WHERE rule_code IN ('cya_bad_3_of_5', 'cya_rise_15_60d', 'cya_rise_30_60d')
            """
        )
        cur.execute(
            """
            DELETE FROM alert_rule_config
            WHERE rule_code IN ('ph_above_7_8', 'ph_above_8_2')
               OR (reading_key = 'ph' AND rule_code IN ('ph_above_7_8', 'ph_above_8_2'))
            """
        )
        cur.execute(
            """
            DELETE FROM trend_rule_config
            WHERE rule_code = 'ph_bad_3_of_5'
            """
        )
        cur.executemany(
            """
            INSERT INTO revenue_rule_config (
                rule_code,
                opportunity_type,
                source_type,
                reading_key,
                trend_type,
                comparator,
                severity,
                severity_rank,
                threshold_value,
                repeat_count,
                window_days,
                enabled,
                season_start_month,
                season_end_month,
                description
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (rule_code) DO NOTHING
            """,
            [
                ("drain_refill_cya_repeat", "drain_refill", "reading_repeat", "cya", None, "gt", "warning", 10, 100, 2, 60, True, None, None, "Repeated high CYA suggests drain/refill"),
                ("filter_clean_trend", "filter_clean", "reading_repeat", "filter_pressure", None, "gte", "warning", 10, 20, 2, 21, True, None, None, "2 recent PSI readings at or above 20 suggest filter clean"),
                ("filter_clean_missing_psi", "filter_clean", "missing_recent_reading", "filter_pressure", None, None, "warning", 10, None, None, 90, True, None, None, "Missing recent PSI reading"),
                ("phosphate_treatment_high", "phosphate_treatment", "latest_reading", "phosphates", None, "gt", "warning", 10, 500, None, 60, True, None, None, "High phosphates suggest treatment"),
                ("chemical_cost_review_high", "chemical_cost_review", "monthly_cost", None, None, "gte", "warning", 10, monthly_chemical_cost_review_threshold, None, 30, True, None, None, "High recent chemical cost"),
            ],
        )
        cur.execute(
            """
            DELETE FROM trend_rule_config
            WHERE rule_code IN ('psi_rise_5_60d', 'psi_rise_8_60d')
               OR (reading_key = 'filter_pressure' AND trend_type = 'baseline_or_window_delta')
            """
        )

        cur.execute(
            """
            CREATE OR REPLACE VIEW current_chemistry_alerts_v AS
            WITH latest AS (
                SELECT
                    r.*,
                    p.name AS pool_name,
                    c.first_name,
                    c.last_name,
                    c.company_name,
                    ROW_NUMBER() OVER (
                        PARTITION BY r.pool_id, r.reading_key
                        ORDER BY r.service_date DESC, r.id DESC
                    ) AS rn
                FROM chemistry_readings r
                JOIN pools p ON p.id = r.pool_id
                JOIN customers c ON c.id = r.customer_id
                WHERE c.is_operationally_active = TRUE
            ),
            matched AS (
                SELECT
                    l.customer_id,
                    l.pool_id,
                    l.reading_key,
                    l.reading_type,
                    l.description,
                    l.value,
                    l.unit_of_measure,
                    l.service_date,
                    l.pool_name,
                    COALESCE(
                        NULLIF(trim(concat_ws(' ', l.first_name, l.last_name)), ''),
                        NULLIF(l.company_name, ''),
                        'Unknown Customer'
                    ) AS customer_name,
                    cfg.rule_code,
                    cfg.severity,
                    cfg.severity_rank,
                    cfg.threshold_value
                FROM latest l
                JOIN alert_rule_config cfg
                  ON cfg.enabled = TRUE
                 AND cfg.reading_key = l.reading_key
                 AND rule_applies_in_month(cfg.season_start_month, cfg.season_end_month, l.service_date::date)
                WHERE l.rn = 1
                  AND (
                      (cfg.comparator = 'lt' AND l.value < cfg.threshold_value)
                      OR (cfg.comparator = 'lte' AND l.value <= cfg.threshold_value)
                      OR (cfg.comparator = 'gt' AND l.value > cfg.threshold_value)
                      OR (cfg.comparator = 'gte' AND l.value >= cfg.threshold_value)
                  )
            )
            SELECT DISTINCT ON (pool_id, reading_key)
                customer_id,
                pool_id,
                customer_name,
                pool_name,
                reading_key,
                reading_type,
                description,
                value,
                unit_of_measure,
                service_date,
                rule_code,
                severity,
                threshold_value
            FROM matched
            ORDER BY pool_id, reading_key, severity_rank DESC, service_date DESC
            """
        )

        cur.execute(
            """
            CREATE OR REPLACE VIEW chemistry_trend_alerts_v AS
            WITH bad_readings AS (
                SELECT
                    tr.rule_code,
                    tr.severity,
                    tr.severity_rank,
                    r.customer_id,
                    r.pool_id,
                    r.reading_key,
                    MAX(r.service_date) AS service_date,
                    COUNT(*) FILTER (
                        WHERE (
                            (tr.comparator = 'lt' AND r.value < tr.threshold_value)
                            OR (tr.comparator = 'lte' AND r.value <= tr.threshold_value)
                            OR (tr.comparator = 'gt' AND r.value > tr.threshold_value)
                            OR (tr.comparator = 'gte' AND r.value >= tr.threshold_value)
                        )
                    ) AS bad_count,
                    tr.sample_size,
                    tr.min_bad_count
                FROM (
                    SELECT
                        r.*,
                        ROW_NUMBER() OVER (
                            PARTITION BY r.pool_id, r.reading_key
                            ORDER BY r.service_date DESC, r.id DESC
                        ) AS rn
                    FROM chemistry_readings r
                    JOIN customers c ON c.id = r.customer_id
                    WHERE c.is_operationally_active = TRUE
                ) r
                JOIN trend_rule_config tr
                  ON tr.enabled = TRUE
                 AND tr.trend_type = 'bad_readings_last_n'
                 AND tr.reading_key = r.reading_key
                 AND rule_applies_in_month(tr.season_start_month, tr.season_end_month, r.service_date::date)
                WHERE r.rn <= COALESCE(tr.sample_size, 5)
                GROUP BY
                    tr.rule_code,
                    tr.severity,
                    tr.severity_rank,
                    r.customer_id,
                    r.pool_id,
                    r.reading_key,
                    tr.sample_size,
                    tr.min_bad_count
                HAVING COUNT(*) >= COALESCE(tr.sample_size, 5)
                   AND COUNT(*) FILTER (
                        WHERE (
                            (tr.comparator = 'lt' AND r.value < tr.threshold_value)
                            OR (tr.comparator = 'lte' AND r.value <= tr.threshold_value)
                            OR (tr.comparator = 'gt' AND r.value > tr.threshold_value)
                            OR (tr.comparator = 'gte' AND r.value >= tr.threshold_value)
                        )
                   ) >= COALESCE(tr.min_bad_count, 3)
            ),
            fc_cya_pairs AS (
                SELECT
                    r.customer_id,
                    r.pool_id,
                    r.service_date,
                    MAX(CASE WHEN r.reading_key = 'free_chlorine' THEN r.value END) AS free_chlorine,
                    MAX(CASE WHEN r.reading_key = 'cya' THEN r.value END) AS cya
                FROM chemistry_readings r
                JOIN customers c ON c.id = r.customer_id
                WHERE c.is_operationally_active = TRUE
                  AND r.reading_key IN ('free_chlorine', 'cya')
                GROUP BY r.customer_id, r.pool_id, r.service_date
                HAVING MAX(CASE WHEN r.reading_key = 'free_chlorine' THEN r.value END) IS NOT NULL
                   AND MAX(CASE WHEN r.reading_key = 'cya' THEN r.value END) IS NOT NULL
                   AND MAX(CASE WHEN r.reading_key = 'cya' THEN r.value END) > 0
            ),
            fc_cya_ratio_bad AS (
                SELECT
                    tr.rule_code,
                    tr.severity,
                    tr.severity_rank,
                    ranked.customer_id,
                    ranked.pool_id,
                    tr.reading_key,
                    MAX(ranked.service_date) AS service_date,
                    MAX(CASE WHEN ranked.rn = 1 THEN ranked.fc_cya_ratio END) AS latest_ratio,
                    tr.threshold_value
                FROM (
                    SELECT
                        p.*,
                        (p.free_chlorine / NULLIF(p.cya, 0)) AS fc_cya_ratio,
                        ROW_NUMBER() OVER (
                            PARTITION BY p.pool_id
                            ORDER BY p.service_date DESC
                        ) AS rn
                    FROM fc_cya_pairs p
                ) ranked
                JOIN trend_rule_config tr
                  ON tr.enabled = TRUE
                 AND tr.trend_type = 'fc_cya_ratio_last_n'
                 AND tr.reading_key = 'fc_cya_ratio'
                 AND rule_applies_in_month(tr.season_start_month, tr.season_end_month, ranked.service_date::date)
                WHERE ranked.rn <= COALESCE(tr.sample_size, 2)
                GROUP BY
                    tr.rule_code,
                    tr.severity,
                    tr.severity_rank,
                    ranked.customer_id,
                    ranked.pool_id,
                    tr.reading_key,
                    tr.threshold_value,
                    tr.sample_size,
                    tr.min_bad_count
                HAVING COUNT(*) >= COALESCE(tr.sample_size, 2)
                   AND COUNT(*) FILTER (
                        WHERE ranked.fc_cya_ratio < COALESCE(tr.threshold_value, 0.075)
                   ) >= COALESCE(tr.min_bad_count, 2)
            ),
            delta_over_days AS (
                SELECT
                    tr.rule_code,
                    tr.severity,
                    tr.severity_rank,
                    c.id AS customer_id,
                    p.id AS pool_id,
                    tr.reading_key,
                    latest.service_date,
                    (latest.value - earliest.value) AS observed_delta,
                    tr.delta_threshold
                FROM trend_rule_config tr
                JOIN pools p ON tr.trend_type = 'delta_over_days'
                JOIN customers c ON c.id = p.customer_id AND c.is_operationally_active = TRUE
                JOIN LATERAL (
                    SELECT r.*
                    FROM chemistry_readings r
                    WHERE r.pool_id = p.id
                      AND r.reading_key = tr.reading_key
                      AND r.service_date >= NOW() - make_interval(days => COALESCE(tr.window_days, 60))
                    ORDER BY r.service_date DESC, r.id DESC
                    LIMIT 1
                ) latest ON TRUE
                JOIN LATERAL (
                    SELECT r.*
                    FROM chemistry_readings r
                    WHERE r.pool_id = p.id
                      AND r.reading_key = tr.reading_key
                      AND r.service_date >= NOW() - make_interval(days => COALESCE(tr.window_days, 60))
                    ORDER BY r.service_date ASC, r.id ASC
                    LIMIT 1
                ) earliest ON TRUE
                WHERE tr.enabled = TRUE
                  AND rule_applies_in_month(tr.season_start_month, tr.season_end_month, latest.service_date::date)
                  AND latest.value IS NOT NULL
                  AND earliest.value IS NOT NULL
                  AND (
                      tr.threshold_value IS NULL
                      OR latest.value >= tr.threshold_value
                  )
                  AND (latest.value - earliest.value) >= COALESCE(tr.delta_threshold, 0)
            ),
            psi_rising AS (
                SELECT
                    tr.rule_code,
                    tr.severity,
                    tr.severity_rank,
                    c.id AS customer_id,
                    p.id AS pool_id,
                    tr.reading_key,
                    latest.service_date,
                    GREATEST(
                        COALESCE(latest.value - earliest.value, 0),
                        COALESCE(latest.value - p.baseline_filter_pressure, 0)
                    ) AS observed_delta,
                    GREATEST(
                        COALESCE(tr.delta_threshold, 0),
                        COALESCE(tr.baseline_delta_threshold, 0)
                    ) AS delta_threshold
                FROM trend_rule_config tr
                JOIN pools p ON tr.trend_type = 'baseline_or_window_delta'
                JOIN customers c ON c.id = p.customer_id AND c.is_operationally_active = TRUE
                JOIN LATERAL (
                    SELECT r.*
                    FROM chemistry_readings r
                    WHERE r.pool_id = p.id
                      AND r.reading_key = tr.reading_key
                      AND r.service_date >= NOW() - make_interval(days => COALESCE(tr.window_days, 60))
                    ORDER BY r.service_date DESC, r.id DESC
                    LIMIT 1
                ) latest ON TRUE
                JOIN LATERAL (
                    SELECT r.*
                    FROM chemistry_readings r
                    WHERE r.pool_id = p.id
                      AND r.reading_key = tr.reading_key
                      AND r.service_date >= NOW() - make_interval(days => COALESCE(tr.window_days, 60))
                    ORDER BY r.service_date ASC, r.id ASC
                    LIMIT 1
                ) earliest ON TRUE
                WHERE tr.enabled = TRUE
                  AND rule_applies_in_month(tr.season_start_month, tr.season_end_month, latest.service_date::date)
                  AND (
                      COALESCE(latest.value - earliest.value, 0) >= COALESCE(tr.delta_threshold, 999999)
                      OR COALESCE(latest.value - p.baseline_filter_pressure, 0) >= COALESCE(tr.baseline_delta_threshold, 999999)
                  )
            ),
            unioned AS (
                SELECT rule_code, severity, severity_rank, customer_id, pool_id, reading_key, service_date,
                       bad_count::NUMERIC AS observed_value, min_bad_count::NUMERIC AS threshold_value
                FROM bad_readings
                UNION ALL
                SELECT rule_code, severity, severity_rank, customer_id, pool_id, reading_key, service_date,
                       latest_ratio AS observed_value, threshold_value
                FROM fc_cya_ratio_bad
                UNION ALL
                SELECT rule_code, severity, severity_rank, customer_id, pool_id, reading_key, service_date,
                       observed_delta, delta_threshold
                FROM delta_over_days
                UNION ALL
                SELECT rule_code, severity, severity_rank, customer_id, pool_id, reading_key, service_date,
                       observed_delta, delta_threshold
                FROM psi_rising
            )
            SELECT
                u.rule_code,
                u.severity,
                u.customer_id,
                u.pool_id,
                COALESCE(NULLIF(trim(concat_ws(' ', c.first_name, c.last_name)), ''), NULLIF(c.company_name, ''), 'Unknown Customer') AS customer_name,
                p.name AS pool_name,
                u.reading_key,
                u.service_date,
                u.observed_value,
                u.threshold_value
            FROM unioned u
            JOIN customers c ON c.id = u.customer_id
            JOIN pools p ON p.id = u.pool_id
            WHERE c.is_operationally_active = TRUE
            """
        )

        cur.execute(
            """
            CREATE OR REPLACE VIEW revenue_opportunities_v AS
            WITH latest_readings AS (
                SELECT
                    r.*,
                    ROW_NUMBER() OVER (
                        PARTITION BY r.pool_id, r.reading_key
                        ORDER BY r.service_date DESC, r.id DESC
                    ) AS rn
                FROM chemistry_readings r
                JOIN customers c ON c.id = r.customer_id
                WHERE c.is_operationally_active = TRUE
            ),
            filter_clean_eligible_pools AS (
                SELECT
                    p.customer_id,
                    p.id AS pool_id
                FROM pools p
                JOIN customers c ON c.id = p.customer_id
                LEFT JOIN LATERAL (
                    SELECT 1 AS has_current_assignment
                    FROM service_location_technician_assignments a
                    WHERE a.source_system = p.source_system
                      AND a.source_service_location_id = p.source_service_location_id
                      AND a.is_deleted = FALSE
                      AND (a.end_date IS NULL OR a.end_date >= CURRENT_DATE)
                    LIMIT 1
                ) assignment ON TRUE
                LEFT JOIN LATERAL (
                    SELECT 1 AS has_recent_route_stop
                    FROM technician_route_stops s
                    WHERE s.source_system = p.source_system
                      AND s.source_service_location_id = p.source_service_location_id
                      AND s.is_skipped = FALSE
                      AND s.service_date >= NOW() - INTERVAL '60 days'
                    LIMIT 1
                ) recent_stop ON TRUE
                LEFT JOIN LATERAL (
                    SELECT MIN(r.service_date) AS first_chemistry_service_date
                    FROM chemistry_readings r
                    WHERE r.pool_id = p.id
                ) history ON TRUE
                LEFT JOIN LATERAL (
                    SELECT COUNT(*) AS high_psi_count
                    FROM chemistry_readings r
                    WHERE r.pool_id = p.id
                      AND r.reading_key = 'filter_pressure'
                      AND r.value > 20
                ) psi ON TRUE
                WHERE c.is_operationally_active = TRUE
                  AND (
                      assignment.has_current_assignment IS NOT NULL
                      OR recent_stop.has_recent_route_stop IS NOT NULL
                  )
                  AND (
                      history.first_chemistry_service_date <= NOW() - INTERVAL '90 days'
                      OR COALESCE(psi.high_psi_count, 0) >= 2
                  )
            ),
            repeated_readings AS (
                SELECT
                    cfg.rule_code,
                    cfg.opportunity_type,
                    r.customer_id,
                    r.pool_id,
                    r.reading_key,
                    COUNT(*) AS observed_count,
                    MAX(r.service_date) AS service_date
                FROM revenue_rule_config cfg
                JOIN chemistry_readings r
                  ON cfg.enabled = TRUE
                 AND cfg.source_type = 'reading_repeat'
                 AND cfg.reading_key = r.reading_key
                 AND r.service_date >= NOW() - make_interval(days => COALESCE(cfg.window_days, 60))
                JOIN customers c ON c.id = r.customer_id
                WHERE c.is_operationally_active = TRUE
                  AND rule_applies_in_month(cfg.season_start_month, cfg.season_end_month, r.service_date::date)
                  AND (
                      (cfg.comparator = 'lt' AND r.value < cfg.threshold_value)
                      OR (cfg.comparator = 'lte' AND r.value <= cfg.threshold_value)
                      OR (cfg.comparator = 'gt' AND r.value > cfg.threshold_value)
                      OR (cfg.comparator = 'gte' AND r.value >= cfg.threshold_value)
                  )
                GROUP BY cfg.rule_code, cfg.opportunity_type, r.customer_id, r.pool_id, r.reading_key, cfg.repeat_count
                HAVING COUNT(*) >= COALESCE(MAX(cfg.repeat_count), 2)
            ),
            trend_reference AS (
                SELECT
                    cfg.rule_code,
                    cfg.opportunity_type,
                    t.customer_id,
                    t.pool_id,
                    cfg.reading_key,
                    COUNT(*) AS observed_count,
                    MAX(t.service_date) AS service_date
                FROM revenue_rule_config cfg
                JOIN chemistry_trend_alerts_v t
                  ON cfg.enabled = TRUE
                 AND cfg.source_type = 'trend_reference'
                 AND cfg.reading_key = t.reading_key
                LEFT JOIN filter_clean_eligible_pools fcep
                  ON fcep.pool_id = t.pool_id
                GROUP BY cfg.rule_code, cfg.opportunity_type, t.customer_id, t.pool_id, cfg.reading_key
                HAVING cfg.opportunity_type <> 'filter_clean' OR MAX(fcep.pool_id) IS NOT NULL
            ),
            latest_reading_match AS (
                SELECT
                    cfg.rule_code,
                    cfg.opportunity_type,
                    lr.customer_id,
                    lr.pool_id,
                    lr.reading_key,
                    1 AS observed_count,
                    lr.service_date
                FROM revenue_rule_config cfg
                JOIN latest_readings lr
                  ON cfg.enabled = TRUE
                 AND cfg.source_type = 'latest_reading'
                 AND cfg.reading_key = lr.reading_key
                 AND lr.rn = 1
                WHERE rule_applies_in_month(cfg.season_start_month, cfg.season_end_month, lr.service_date::date)
                  AND (
                      (cfg.comparator = 'lt' AND lr.value < cfg.threshold_value)
                      OR (cfg.comparator = 'lte' AND lr.value <= cfg.threshold_value)
                      OR (cfg.comparator = 'gt' AND lr.value > cfg.threshold_value)
                      OR (cfg.comparator = 'gte' AND lr.value >= cfg.threshold_value)
                  )
            ),
            missing_recent_reading AS (
                SELECT
                    cfg.rule_code,
                    cfg.opportunity_type,
                    p.customer_id,
                    p.id AS pool_id,
                    cfg.reading_key,
                    0 AS observed_count,
                    NULL::TIMESTAMPTZ AS service_date
                FROM revenue_rule_config cfg
                JOIN pools p ON cfg.enabled = TRUE AND cfg.source_type = 'missing_recent_reading'
                JOIN customers c ON c.id = p.customer_id
                LEFT JOIN filter_clean_eligible_pools fcep ON fcep.pool_id = p.id
                WHERE c.is_operationally_active = TRUE
                  AND (cfg.opportunity_type <> 'filter_clean' OR fcep.pool_id IS NOT NULL)
                  AND NOT EXISTS (
                      SELECT 1
                      FROM chemistry_readings r
                      WHERE r.pool_id = p.id
                        AND r.reading_key = cfg.reading_key
                        AND r.service_date >= NOW() - make_interval(days => COALESCE(cfg.window_days, 90))
                  )
            ),
            recent_completed_filter_cleans AS (
                SELECT DISTINCT
                    p.customer_id,
                    p.id AS pool_id
                FROM sk_work_order w
                JOIN pools p
                  ON p.source_system = w.source_system
                 AND p.source_service_location_id = w.source_service_location_id
                LEFT JOIN sk_work_order_type wt
                  ON wt.source_system = w.source_system
                 AND wt.source_work_order_type_id = w.source_work_order_type_id
                WHERE COALESCE(w.is_deleted, FALSE) = FALSE
                  AND w.service_date >= NOW() - make_interval(days => 90)
                  AND w.complete_time IS NOT NULL
                  AND w.complete_time >= TIMESTAMPTZ '2011-01-01 00:00:00+00'
                  AND (
                      lower(COALESCE(wt.description, '')) = 'filter clean'
                      OR lower(COALESCE(w.work_needed, '')) LIKE '%filter clean%'
                  )
            ),
            monthly_cost AS (
                SELECT
                    cfg.rule_code,
                    cfg.opportunity_type,
                    d.customer_id,
                    d.pool_id,
                    NULL::TEXT AS reading_key,
                    SUM(COALESCE(d.estimated_cost, 0)) AS observed_count,
                    MAX(d.service_date) AS service_date
                FROM revenue_rule_config cfg
                JOIN chemical_dose_events d
                  ON cfg.enabled = TRUE
                 AND cfg.source_type = 'monthly_cost'
                 AND d.service_date >= NOW() - make_interval(days => COALESCE(cfg.window_days, 30))
                JOIN customers c ON c.id = d.customer_id
                WHERE c.is_operationally_active = TRUE
                GROUP BY cfg.rule_code, cfg.opportunity_type, d.customer_id, d.pool_id, cfg.threshold_value
                HAVING SUM(COALESCE(d.estimated_cost, 0)) >= MAX(COALESCE(cfg.threshold_value, 0))
            ),
            unioned AS (
                SELECT * FROM repeated_readings
                UNION ALL
                SELECT * FROM trend_reference
                UNION ALL
                SELECT * FROM latest_reading_match
                UNION ALL
                SELECT * FROM missing_recent_reading
                UNION ALL
                SELECT * FROM monthly_cost
            )
            SELECT
                u.rule_code,
                u.opportunity_type,
                u.customer_id,
                u.pool_id,
                COALESCE(NULLIF(trim(concat_ws(' ', c.first_name, c.last_name)), ''), NULLIF(c.company_name, ''), 'Unknown Customer') AS customer_name,
                p.name AS pool_name,
                u.reading_key,
                u.observed_count,
                u.service_date
            FROM unioned u
            JOIN customers c ON c.id = u.customer_id
            LEFT JOIN pools p ON p.id = u.pool_id
            LEFT JOIN recent_completed_filter_cleans fc ON fc.pool_id = u.pool_id
            WHERE c.is_operationally_active = TRUE
              AND NOT (
                  u.opportunity_type = 'filter_clean'
                  AND fc.pool_id IS NOT NULL
              )
            """
        )

        cur.execute(
            """
            CREATE OR REPLACE VIEW dashboard_summary_v AS
            SELECT
                NOW() AS generated_at,
                (SELECT completed_at FROM ingest_pipeline_runs WHERE success = TRUE ORDER BY started_at DESC LIMIT 1) AS last_successful_pipeline_at,
                (SELECT COUNT(*) FROM customers WHERE is_operationally_active = TRUE) AS active_customer_count,
                (SELECT COUNT(*) FROM pools p JOIN customers c ON c.id = p.customer_id WHERE c.is_operationally_active = TRUE) AS active_pool_count,
                (SELECT COUNT(DISTINCT customer_id) FROM current_chemistry_alerts_v) AS customers_with_current_alerts,
                (SELECT COUNT(*) FROM current_chemistry_alerts_v WHERE severity = 'critical') AS critical_current_alert_count,
                (SELECT COUNT(*) FROM chemistry_trend_alerts_v) AS chemistry_trend_alert_count,
                (SELECT COUNT(*) FROM revenue_opportunities_v) AS revenue_opportunity_count
            """
        )
