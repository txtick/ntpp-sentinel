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

        cur.execute(
            """
            CREATE OR REPLACE FUNCTION customer_alert_eligible(
                customer_id BIGINT,
                first_service_date TIMESTAMPTZ,
                customer_raw_json JSONB
            ) RETURNS BOOLEAN
            LANGUAGE SQL
            STABLE
            AS $$
                SELECT
                    first_service_date IS NOT NULL
                    AND first_service_date <= NOW() - INTERVAL '14 days'
                    AND COALESCE(customer_raw_json::text, '') NOT ILIKE '%service-only%'
                    AND NOT customer_has_tag(customer_raw_json, 'no-sentinel-alerts')
                    AND NOT EXISTS (
                        SELECT 1
                        FROM service_location_technician_assignments a
                        WHERE a.customer_id = customer_alert_eligible.customer_id
                          AND a.is_deleted = FALSE
                          AND a.end_date IS NOT NULL
                          AND a.end_date::date >= CURRENT_DATE
                          AND a.end_date::date <= CURRENT_DATE + 30
                    )
            $$;
            """
        )

        cur.execute(
            """
            CREATE OR REPLACE FUNCTION customer_has_tag(
                customer_raw_json JSONB,
                target_tag TEXT
            ) RETURNS BOOLEAN
            LANGUAGE SQL
            STABLE
            AS $$
                SELECT CASE
                    WHEN customer_raw_json IS NULL OR target_tag IS NULL OR btrim(target_tag) = '' THEN FALSE
                    ELSE EXISTS (
                        SELECT 1
                        FROM jsonb_array_elements(
                            CASE
                                WHEN jsonb_typeof(customer_raw_json->'tags') = 'array' THEN customer_raw_json->'tags'
                                WHEN jsonb_typeof(customer_raw_json->'Tags') = 'array' THEN customer_raw_json->'Tags'
                                ELSE '[]'::jsonb
                            END
                        ) AS tag
                        WHERE lower(
                            COALESCE(
                                NULLIF(tag->>'name', ''),
                                NULLIF(tag->>'Name', ''),
                                NULLIF(trim(BOTH '"' FROM tag::text), '')
                            )
                        ) = lower(btrim(target_tag))
                    )
                    OR lower(COALESCE(customer_raw_json::text, '')) LIKE ('%' || lower(btrim(target_tag)) || '%')
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
                season_start_month = EXCLUDED.season_start_month,
                season_end_month = EXCLUDED.season_end_month,
                description = EXCLUDED.description
            """,
            [
                ("cya_above_100", "cya", "gt", "critical", 20, 100, True, None, None, "CYA above 100 ppm"),
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
                sample_size = EXCLUDED.sample_size,
                min_bad_count = EXCLUDED.min_bad_count,
                window_days = EXCLUDED.window_days,
                delta_threshold = EXCLUDED.delta_threshold,
                baseline_delta_threshold = EXCLUDED.baseline_delta_threshold,
                season_start_month = EXCLUDED.season_start_month,
                season_end_month = EXCLUDED.season_end_month,
                description = EXCLUDED.description
            """,
            [
                ("fc_cya_ratio_bad_2wk", "fc_cya_ratio", "fc_cya_ratio_last_n", "lt", "warning", 10, 0.075, 2, 2, 21, None, None, True, None, None, "FC:CYA ratio below 7.5% on last 2 paired readings"),
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
            WHERE rule_code IN ('cya_above_80', 'phosphates_above_500', 'phosphates_above_1000')
            """
        )
        cur.execute(
            """
            DELETE FROM trend_rule_config
            WHERE rule_code IN (
                'cya_bad_3_of_5',
                'cya_rise_15_60d',
                'cya_rise_30_60d',
                'fc_zero_2_of_2_14d',
                'cya_rise_above_50_15_60d',
                'phosphates_bad_3_of_5'
            )
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
            ON CONFLICT (rule_code) DO UPDATE
            SET
                opportunity_type = EXCLUDED.opportunity_type,
                source_type = EXCLUDED.source_type,
                reading_key = EXCLUDED.reading_key,
                trend_type = EXCLUDED.trend_type,
                comparator = EXCLUDED.comparator,
                severity = EXCLUDED.severity,
                severity_rank = EXCLUDED.severity_rank,
                repeat_count = EXCLUDED.repeat_count,
                window_days = EXCLUDED.window_days,
                season_start_month = EXCLUDED.season_start_month,
                season_end_month = EXCLUDED.season_end_month,
                description = EXCLUDED.description
            """,
            [
                ("drain_refill_cya_repeat", "drain_refill", "reading_repeat", "cya", None, "gt", "warning", 10, 100, 2, 60, True, None, None, "Repeated high CYA suggests drain/refill"),
                ("filter_clean_trend", "filter_clean", "reading_repeat", "filter_pressure", None, "gte", "warning", 10, 20, 2, 21, True, None, None, "2 recent PSI readings at or above 20 suggest filter clean"),
                ("filter_clean_missing_psi", "filter_clean", "missing_recent_reading", "filter_pressure", None, None, "warning", 10, None, None, 90, True, None, None, "Missing recent PSI reading"),
                ("freedom_filter_clean_not_scheduled", "filter_clean", "scheduled_work_missing", None, None, None, "warning", 10, None, None, 30, True, None, None, "Freedom customer has no upcoming filter clean scheduled"),
                ("chemical_cost_review_high", "chemical_cost_review", "monthly_cost", None, None, "gte", "warning", 10, monthly_chemical_cost_review_threshold, None, 30, True, None, None, "High recent chemical cost"),
            ],
        )
        cur.execute(
            """
            DELETE FROM revenue_rule_config
            WHERE rule_code = 'phosphate_treatment_high'
            """
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
            WITH customer_service_history AS (
                SELECT
                    r.customer_id,
                    MIN(r.service_date) AS first_service_date
                FROM chemistry_readings r
                GROUP BY r.customer_id
            ),
            latest AS (
                SELECT
                    r.*,
                    p.name AS pool_name,
                    c.first_name,
                    c.last_name,
                    c.company_name,
                    c.raw_json AS customer_raw_json,
                    csh.first_service_date,
                    ROW_NUMBER() OVER (
                        PARTITION BY r.pool_id, r.reading_key
                        ORDER BY r.service_date DESC, r.id DESC
                    ) AS rn
                FROM chemistry_readings r
                JOIN pools p ON p.id = r.pool_id
                JOIN customers c ON c.id = r.customer_id
                LEFT JOIN customer_service_history csh ON csh.customer_id = c.id
                WHERE c.is_operationally_active = TRUE
                  AND customer_alert_eligible(c.id, csh.first_service_date, c.raw_json)
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
            WITH customer_service_history AS (
                SELECT
                    r.customer_id,
                    MIN(r.service_date) AS first_service_date
                FROM chemistry_readings r
                GROUP BY r.customer_id
            ),
            bad_readings AS (
                SELECT
                    tr.rule_code,
                    tr.severity,
                    tr.severity_rank,
                    r.customer_id,
                    r.pool_id,
                    r.reading_key,
                    MAX(r.service_date) AS service_date,
                    MAX(CASE WHEN r.rn = 1 THEN r.value END) AS latest_value,
                    COUNT(*) FILTER (
                        WHERE (
                            (tr.comparator = 'lt' AND r.value < tr.threshold_value)
                            OR (tr.comparator = 'lte' AND r.value <= tr.threshold_value)
                            OR (tr.comparator = 'gt' AND r.value > tr.threshold_value)
                            OR (tr.comparator = 'gte' AND r.value >= tr.threshold_value)
                        )
                    ) AS bad_count,
                    tr.threshold_value,
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
                    LEFT JOIN customer_service_history csh ON csh.customer_id = c.id
                    WHERE c.is_operationally_active = TRUE
                      AND customer_alert_eligible(c.id, csh.first_service_date, c.raw_json)
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
                    tr.threshold_value,
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
                LEFT JOIN customer_service_history csh ON csh.customer_id = c.id
                WHERE c.is_operationally_active = TRUE
                  AND customer_alert_eligible(c.id, csh.first_service_date, c.raw_json)
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
                LEFT JOIN customer_service_history csh ON csh.customer_id = c.id
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
                  AND customer_alert_eligible(c.id, csh.first_service_date, c.raw_json)
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
                LEFT JOIN customer_service_history csh ON csh.customer_id = c.id
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
                  AND customer_alert_eligible(c.id, csh.first_service_date, c.raw_json)
                  AND rule_applies_in_month(tr.season_start_month, tr.season_end_month, latest.service_date::date)
                  AND (
                      COALESCE(latest.value - earliest.value, 0) >= COALESCE(tr.delta_threshold, 999999)
                      OR COALESCE(latest.value - p.baseline_filter_pressure, 0) >= COALESCE(tr.baseline_delta_threshold, 999999)
                  )
            ),
            unioned AS (
                SELECT rule_code, severity, severity_rank, customer_id, pool_id, reading_key, service_date,
                       latest_value AS observed_value, threshold_value
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
            WITH customer_service_history AS (
                SELECT
                    r.customer_id,
                    MIN(r.service_date) AS first_service_date
                FROM chemistry_readings r
                GROUP BY r.customer_id
            ),
            latest_readings AS (
                SELECT
                    r.*,
                    ROW_NUMBER() OVER (
                        PARTITION BY r.pool_id, r.reading_key
                        ORDER BY r.service_date DESC, r.id DESC
                    ) AS rn
                FROM chemistry_readings r
                JOIN customers c ON c.id = r.customer_id
                WHERE c.is_operationally_active = TRUE
                  AND customer_alert_eligible(
                      c.id,
                      (SELECT csh.first_service_date FROM customer_service_history csh WHERE csh.customer_id = c.id),
                      c.raw_json
                  )
            ),
            filter_clean_eligible_pools AS (
                SELECT
                    p.customer_id,
                    p.id AS pool_id
                FROM pools p
                JOIN customers c ON c.id = p.customer_id
                LEFT JOIN customer_service_history csh ON csh.customer_id = c.id
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
                  AND customer_alert_eligible(c.id, csh.first_service_date, c.raw_json)
                  AND NOT customer_has_tag(c.raw_json, 'filter-sand')
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
                    MAX(r.value) AS observed_value,
                    MAX(cfg.threshold_value) AS threshold_value,
                    MAX(r.service_date) AS service_date
                FROM revenue_rule_config cfg
                JOIN chemistry_readings r
                  ON cfg.enabled = TRUE
                 AND cfg.source_type = 'reading_repeat'
                 AND cfg.reading_key = r.reading_key
                 AND r.service_date >= NOW() - make_interval(days => COALESCE(cfg.window_days, 60))
                JOIN customers c ON c.id = r.customer_id
                LEFT JOIN filter_clean_eligible_pools fcep
                  ON fcep.pool_id = r.pool_id
                WHERE c.is_operationally_active = TRUE
                  AND customer_alert_eligible(
                      c.id,
                      (SELECT csh.first_service_date FROM customer_service_history csh WHERE csh.customer_id = c.id),
                      c.raw_json
                  )
                  AND rule_applies_in_month(cfg.season_start_month, cfg.season_end_month, r.service_date::date)
                  AND (
                      (cfg.comparator = 'lt' AND r.value < cfg.threshold_value)
                      OR (cfg.comparator = 'lte' AND r.value <= cfg.threshold_value)
                      OR (cfg.comparator = 'gt' AND r.value > cfg.threshold_value)
                      OR (cfg.comparator = 'gte' AND r.value >= cfg.threshold_value)
                  )
                GROUP BY cfg.rule_code, cfg.opportunity_type, r.customer_id, r.pool_id, r.reading_key, cfg.repeat_count
                HAVING COUNT(*) >= COALESCE(MAX(cfg.repeat_count), 2)
                   AND (cfg.opportunity_type <> 'filter_clean' OR MAX(fcep.pool_id) IS NOT NULL)
            ),
            trend_reference AS (
                SELECT
                    cfg.rule_code,
                    cfg.opportunity_type,
                    t.customer_id,
                    t.pool_id,
                    cfg.reading_key,
                    COUNT(*) AS observed_count,
                    MAX(t.observed_value) AS observed_value,
                    MAX(t.threshold_value) AS threshold_value,
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
                    lr.value AS observed_value,
                    cfg.threshold_value AS threshold_value,
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
                    NULL::NUMERIC AS observed_value,
                    cfg.threshold_value AS threshold_value,
                    NULL::TIMESTAMPTZ AS service_date
                FROM revenue_rule_config cfg
                JOIN pools p ON cfg.enabled = TRUE AND cfg.source_type = 'missing_recent_reading'
                JOIN customers c ON c.id = p.customer_id
                LEFT JOIN customer_service_history csh ON csh.customer_id = c.id
                LEFT JOIN filter_clean_eligible_pools fcep ON fcep.pool_id = p.id
                WHERE c.is_operationally_active = TRUE
                  AND customer_alert_eligible(c.id, csh.first_service_date, c.raw_json)
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
                  AND (
                      lower(COALESCE(wt.description, '')) = 'filter clean'
                      OR lower(COALESCE(w.work_needed, '')) LIKE '%filter clean%'
                  )
                  AND w.service_date >= NOW() - make_interval(days => 90)
                  AND w.service_date < CURRENT_DATE
            ),
            upcoming_filter_cleans AS (
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
                  AND (
                      lower(COALESCE(wt.description, '')) = 'filter clean'
                      OR lower(COALESCE(w.work_needed, '')) LIKE '%filter clean%'
                  )
                  AND (w.complete_time IS NULL OR w.complete_time < TIMESTAMPTZ '2011-01-01 00:00:00+00')
                  AND w.service_date >= CURRENT_DATE
            ),
            freedom_missing_scheduled_filter_clean AS (
                SELECT
                    cfg.rule_code,
                    cfg.opportunity_type,
                    p.customer_id,
                    p.id AS pool_id,
                    NULL::TEXT AS reading_key,
                    0 AS observed_count,
                    NULL::NUMERIC AS observed_value,
                    cfg.threshold_value AS threshold_value,
                    NULL::TIMESTAMPTZ AS service_date
                FROM revenue_rule_config cfg
                JOIN pools p ON cfg.enabled = TRUE AND cfg.source_type = 'scheduled_work_missing'
                JOIN customers c ON c.id = p.customer_id
                LEFT JOIN customer_service_history csh ON csh.customer_id = c.id
                LEFT JOIN filter_clean_eligible_pools fcep ON fcep.pool_id = p.id
                LEFT JOIN upcoming_filter_cleans fc ON fc.pool_id = p.id
                WHERE c.is_operationally_active = TRUE
                  AND customer_alert_eligible(c.id, csh.first_service_date, c.raw_json)
                  AND cfg.opportunity_type = 'filter_clean'
                  AND fcep.pool_id IS NOT NULL
                  AND fc.pool_id IS NULL
                  AND customer_has_tag(c.raw_json, 'freedom')
            ),
            monthly_cost AS (
                SELECT
                    cfg.rule_code,
                    cfg.opportunity_type,
                    d.customer_id,
                    d.pool_id,
                    NULL::TEXT AS reading_key,
                    SUM(COALESCE(d.estimated_cost, 0)) AS observed_count,
                    SUM(COALESCE(d.estimated_cost, 0)) AS observed_value,
                    MAX(cfg.threshold_value) AS threshold_value,
                    MAX(d.service_date) AS service_date
                FROM revenue_rule_config cfg
                JOIN chemical_dose_events d
                  ON cfg.enabled = TRUE
                 AND cfg.source_type = 'monthly_cost'
                 AND d.service_date >= NOW() - make_interval(days => COALESCE(cfg.window_days, 30))
                JOIN customers c ON c.id = d.customer_id
                WHERE c.is_operationally_active = TRUE
                  AND customer_alert_eligible(
                      c.id,
                      (SELECT csh.first_service_date FROM customer_service_history csh WHERE csh.customer_id = c.id),
                      c.raw_json
                  )
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
                SELECT * FROM freedom_missing_scheduled_filter_clean
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
                u.service_date,
                u.observed_value,
                u.threshold_value
            FROM unioned u
            JOIN customers c ON c.id = u.customer_id
            LEFT JOIN pools p ON p.id = u.pool_id
            LEFT JOIN recent_completed_filter_cleans recent_fc ON recent_fc.pool_id = u.pool_id
            LEFT JOIN upcoming_filter_cleans upcoming_fc ON upcoming_fc.pool_id = u.pool_id
            WHERE c.is_operationally_active = TRUE
              AND customer_alert_eligible(
                  c.id,
                  (SELECT csh.first_service_date FROM customer_service_history csh WHERE csh.customer_id = c.id),
                  c.raw_json
              )
              AND NOT (
                  u.opportunity_type = 'filter_clean'
                  AND (
                      upcoming_fc.pool_id IS NOT NULL
                      OR recent_fc.pool_id IS NOT NULL
                  )
              )
            """
        )

        cur.execute(
            """
            CREATE OR REPLACE VIEW problem_pools_v AS
            WITH monthly_chemical_costs AS (
                SELECT
                    d.customer_id,
                    d.pool_id,
                    SUM(COALESCE(d.estimated_cost, 0))::NUMERIC(12, 2) AS monthly_chemical_cost,
                    MAX(d.service_date) AS latest_chemical_service_date
                FROM chemical_dose_events d
                WHERE d.service_date >= NOW() - INTERVAL '30 days'
                GROUP BY d.customer_id, d.pool_id
            ),
            current_assignments AS (
                SELECT
                    a.source_system,
                    a.source_service_location_id,
                    COALESCE(
                        NULLIF(trim(concat_ws(' ', t.first_name, t.last_name)), ''),
                        NULLIF(t.username, ''),
                        t.source_account_id
                    ) AS technician_name,
                    concat_ws(' · ', a.day_of_week, a.frequency) AS technician_route,
                    ROW_NUMBER() OVER (
                        PARTITION BY a.source_system, a.source_service_location_id
                        ORDER BY a.sequence ASC NULLS LAST, a.start_date DESC NULLS LAST, a.id DESC
                    ) AS rn
                FROM service_location_technician_assignments a
                JOIN technicians t ON t.id = a.technician_id
                WHERE a.is_deleted = FALSE
                  AND (a.end_date IS NULL OR a.end_date >= CURRENT_DATE)
            ),
            recent_service_techs AS (
                SELECT
                    s.source_system,
                    s.source_service_location_id,
                    COALESCE(
                        NULLIF(trim(concat_ws(' ', t.first_name, t.last_name)), ''),
                        NULLIF(t.username, ''),
                        t.source_account_id
                    ) AS technician_name,
                    ROW_NUMBER() OVER (
                        PARTITION BY s.source_system, s.source_service_location_id
                        ORDER BY s.service_date DESC, s.sequence ASC NULLS LAST, s.id DESC
                    ) AS rn
                FROM technician_route_stops s
                JOIN technicians t ON t.id = s.technician_id
                WHERE s.is_skipped = FALSE
            ),
            base AS (
                SELECT
                    c.id AS customer_id,
                    COALESCE(
                        NULLIF(trim(concat_ws(' ', c.first_name, c.last_name)), ''),
                        NULLIF(c.company_name, ''),
                        'Unknown Customer'
                    ) AS customer_name,
                    p.id AS pool_id,
                    p.name AS pool_name,
                    p.address AS pool_address,
                    COALESCE(ca.technician_name, rs.technician_name) AS technician_name,
                    ca.technician_route,
                    sl.rate AS service_rate,
                    sl.rate_type AS service_rate_type,
                    /* Problem Pools works off a monthly rate. We convert a few known cadence labels here,
                       then fall back to the imported ServiceLocation.Rate because current pricing workflows
                       already treat that Skimmer field as the operative service rate. */
                    CASE
                        WHEN sl.rate IS NULL OR sl.rate <= 0 THEN NULL::NUMERIC
                        WHEN lower(COALESCE(sl.rate_type, '')) IN ('weekly', 'week', 'per_week', 'per week') THEN ROUND((sl.rate * 52.0 / 12.0)::NUMERIC, 2)
                        WHEN lower(COALESCE(sl.rate_type, '')) IN ('biweekly', 'bi-weekly', 'every_other_week', 'every other week') THEN ROUND((sl.rate * 26.0 / 12.0)::NUMERIC, 2)
                        WHEN lower(COALESCE(sl.rate_type, '')) IN ('twice_monthly', 'semi_monthly', 'semi-monthly', 'semimonthly') THEN ROUND((sl.rate * 2.0)::NUMERIC, 2)
                        WHEN lower(COALESCE(sl.rate_type, '')) IN ('quarterly', 'quarter') THEN ROUND((sl.rate / 3.0)::NUMERIC, 2)
                        ELSE ROUND(sl.rate::NUMERIC, 2)
                    END AS monthly_service_rate,
                    COALESCE(mcc.monthly_chemical_cost, 0)::NUMERIC(12, 2) AS monthly_chemical_cost,
                    mcc.latest_chemical_service_date
                FROM pools p
                JOIN customers c ON c.id = p.customer_id
                LEFT JOIN sk_service_location sl
                  ON sl.source_system = p.source_system
                 AND sl.source_location_id = p.source_service_location_id
                LEFT JOIN monthly_chemical_costs mcc
                  ON mcc.customer_id = p.customer_id
                 AND mcc.pool_id = p.id
                LEFT JOIN current_assignments ca
                  ON ca.source_system = p.source_system
                 AND ca.source_service_location_id = p.source_service_location_id
                 AND ca.rn = 1
                LEFT JOIN recent_service_techs rs
                  ON rs.source_system = p.source_system
                 AND rs.source_service_location_id = p.source_service_location_id
                 AND rs.rn = 1
                WHERE c.is_operationally_active = TRUE
                  AND p.is_operationally_active = TRUE
            ),
            scored AS (
                SELECT
                    base.*,
                    /* chemical_percent = monthly_chemical_cost / monthly_service_rate, displayed as a percentage. */
                    CASE
                        WHEN base.monthly_service_rate IS NULL OR base.monthly_service_rate <= 0 THEN NULL::NUMERIC
                        ELSE ROUND(((base.monthly_chemical_cost / base.monthly_service_rate) * 100.0)::NUMERIC, 1)
                    END AS chemical_percent,
                    /* monthly_leak = max(0, monthly_chemical_cost - (monthly_service_rate * 20%)). */
                    CASE
                        WHEN base.monthly_service_rate IS NULL OR base.monthly_service_rate <= 0 THEN 0::NUMERIC(12, 2)
                        ELSE ROUND(GREATEST(0, base.monthly_chemical_cost - (base.monthly_service_rate * 0.20))::NUMERIC, 2)
                    END AS monthly_leak
                FROM base
            )
            SELECT
                s.customer_id,
                s.customer_name,
                s.pool_id,
                s.pool_name,
                s.pool_address,
                s.technician_name,
                s.technician_route,
                s.service_rate_type,
                s.monthly_service_rate,
                s.monthly_chemical_cost,
                s.chemical_percent,
                CASE
                    WHEN s.monthly_service_rate IS NULL OR s.monthly_service_rate <= 0 THEN 'Missing Rate'
                    WHEN s.chemical_percent >= 35 THEN 'Critical'
                    WHEN s.chemical_percent >= 25 THEN 'Problem'
                    WHEN s.chemical_percent >= 20 THEN 'Watch'
                    ELSE 'Healthy'
                END AS flag,
                CASE
                    WHEN s.monthly_service_rate IS NULL OR s.monthly_service_rate <= 0 THEN 4
                    WHEN s.chemical_percent >= 35 THEN 0
                    WHEN s.chemical_percent >= 25 THEN 1
                    WHEN s.chemical_percent >= 20 THEN 2
                    ELSE 3
                END AS flag_rank,
                s.monthly_leak,
                CASE
                    WHEN s.monthly_service_rate IS NULL OR s.monthly_service_rate <= 0 THEN 'Add service rate'
                    WHEN s.chemical_percent >= 35 THEN 'Immediate review: treatment, price increase, or service adjustment'
                    WHEN s.chemical_percent >= 25 THEN 'Review / recommend treatment'
                    WHEN s.chemical_percent >= 20 THEN 'Monitor'
                    ELSE 'Healthy'
                END AS suggested_action,
                s.latest_chemical_service_date
            FROM scored s
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
                (SELECT COUNT(DISTINCT customer_id) FROM alert_instances WHERE status <> 'cleared' AND category = 'pool') AS customers_with_current_alerts,
                (SELECT COUNT(*) FROM alert_instances WHERE status <> 'cleared' AND severity = 'critical') AS critical_current_alert_count,
                (SELECT COUNT(*) FROM alert_instances WHERE status <> 'cleared' AND category = 'process') AS chemistry_trend_alert_count,
                (SELECT COUNT(*) FROM alert_instances WHERE status <> 'cleared' AND category = 'revenue') AS revenue_opportunity_count
            """
        )
