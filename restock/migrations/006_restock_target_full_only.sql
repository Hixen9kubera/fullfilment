-- Fix: restock target debe ser solo a FULL, sin restar Odoo
-- Boost de demanda: si tendencia es alcista, usar avg_14d en vez de avg_45d_real

DROP FUNCTION IF EXISTS get_restock_table(text);

CREATE OR REPLACE FUNCTION get_restock_table(p_cuenta text DEFAULT NULL)
RETURNS TABLE (
  sku               text,
  cuenta            text,
  title             text,
  size_category     text,
  logistic_type     text,
  item_status       text,
  stock_full        integer,
  stock_odoo        integer,
  price             numeric,
  avg_daily         numeric,
  avg_daily_real    numeric,
  days_full         numeric,
  days_total        numeric,
  restock_suggested integer,
  signal            text,
  trend             text,
  series_14d        jsonb
) LANGUAGE sql STABLE AS $$
  WITH latest_stock AS (
    SELECT DISTINCT ON (ds.cuenta, ds.sku)
      ds.cuenta, ds.sku, ds.size_category, ds.logistic_type,
      ds.status AS item_status, ds.stock_full, ds.stock_odoo, ds.price
    FROM daily_stock ds
    WHERE (p_cuenta IS NULL OR ds.cuenta = p_cuenta)
      AND ds.sku IS NOT NULL AND ds.sku <> ''
    ORDER BY ds.cuenta, ds.sku, ds.date DESC
  ),
  latest_title AS (
    SELECT DISTINCT ON (s.sku) s.sku, s.title
    FROM daily_sales s
    WHERE s.title IS NOT NULL AND s.sku IS NOT NULL AND s.sku <> ''
    ORDER BY s.sku, s.date DESC
  ),
  sales_window AS (
    SELECT
      s.cuenta, s.sku,
      AVG(s.units_sold)::numeric(10,4) AS avg_daily,
      AVG(s.units_sold) FILTER (
        WHERE COALESCE(st.stock_full, 0) + COALESCE(st.stock_odoo, 0) > 0
      )::numeric(10,4) AS avg_daily_real,
      AVG(s.units_sold) FILTER (WHERE s.date >= CURRENT_DATE - 14)::numeric(10,4) AS avg_14d,
      AVG(s.units_sold) FILTER (WHERE s.date BETWEEN CURRENT_DATE - 28 AND CURRENT_DATE - 15)::numeric(10,4) AS avg_prev_14d
    FROM daily_sales s
    LEFT JOIN daily_stock st
           ON st.sku = s.sku AND st.date = s.date AND st.cuenta = s.cuenta
    WHERE (p_cuenta IS NULL OR s.cuenta = p_cuenta)
      AND s.date >= CURRENT_DATE - 45
      AND s.sku IS NOT NULL AND s.sku <> ''
    GROUP BY s.cuenta, s.sku
  ),
  combined_stock AS (
    SELECT
      ls.sku,
      CASE WHEN p_cuenta IS NULL THEN 'AMBAS' ELSE ls.cuenta END AS cuenta,
      MAX(lt.title) AS title,
      MAX(ls.size_category) AS size_category,
      MAX(ls.logistic_type) AS logistic_type,
      MAX(ls.item_status) AS item_status,
      SUM(ls.stock_full) AS stock_full,
      MAX(ls.stock_odoo) AS stock_odoo,
      MAX(ls.price) AS price
    FROM latest_stock ls
    LEFT JOIN latest_title lt ON lt.sku = ls.sku
    GROUP BY ls.sku, CASE WHEN p_cuenta IS NULL THEN 'AMBAS' ELSE ls.cuenta END
  ),
  combined_sales AS (
    SELECT
      sw.sku,
      CASE WHEN p_cuenta IS NULL THEN 'AMBAS' ELSE sw.cuenta END AS cuenta,
      AVG(sw.avg_daily)::numeric(10,4) AS avg_daily,
      COALESCE(
        NULLIF(AVG(sw.avg_daily_real)::numeric(10,4), 0),
        AVG(sw.avg_daily)::numeric(10,4)
      ) AS avg_daily_real,
      AVG(sw.avg_14d)::numeric(10,4) AS avg_14d,
      AVG(sw.avg_prev_14d)::numeric(10,4) AS avg_prev_14d
    FROM sales_window sw
    GROUP BY sw.sku, CASE WHEN p_cuenta IS NULL THEN 'AMBAS' ELSE sw.cuenta END
  ),
  series_data AS (
    SELECT
      s.sku,
      CASE WHEN p_cuenta IS NULL THEN 'AMBAS' ELSE s.cuenta END AS cuenta,
      jsonb_agg(jsonb_build_object('date', s.date, 'units', s.units_sold) ORDER BY s.date) AS series_14d
    FROM daily_sales s
    WHERE (p_cuenta IS NULL OR s.cuenta = p_cuenta)
      AND s.date >= CURRENT_DATE - 14
      AND s.sku IS NOT NULL AND s.sku <> ''
    GROUP BY s.sku, CASE WHEN p_cuenta IS NULL THEN 'AMBAS' ELSE s.cuenta END
  ),
  -- target_avg: para tendencia alcista usar avg_14d (refleja momentum), si no avg_daily_real
  demand_target AS (
    SELECT
      cs.sku, cs.cuenta,
      cs.avg_daily, cs.avg_daily_real, cs.avg_14d, cs.avg_prev_14d,
      CASE
        WHEN COALESCE(cs.avg_prev_14d, 0) = 0 AND COALESCE(cs.avg_14d, 0) > 0
             THEN GREATEST(COALESCE(cs.avg_daily_real, 0), cs.avg_14d)
        WHEN COALESCE(cs.avg_prev_14d, 0) > 0 AND cs.avg_14d / cs.avg_prev_14d >= 1.15
             THEN GREATEST(COALESCE(cs.avg_daily_real, 0), cs.avg_14d)
        ELSE COALESCE(cs.avg_daily_real, 0)
      END AS target_avg
    FROM combined_sales cs
  ),
  computed AS (
    SELECT
      cs.sku, cs.cuenta, cs.title,
      cs.size_category, cs.logistic_type, cs.item_status,
      cs.stock_full::integer, cs.stock_odoo::integer, cs.price,
      COALESCE(dt.avg_daily, 0)::numeric(10,4)      AS avg_daily,
      COALESCE(dt.avg_daily_real, 0)::numeric(10,4) AS avg_daily_real,
      CASE WHEN COALESCE(dt.avg_daily_real, 0) > 0
           THEN (cs.stock_full::numeric / dt.avg_daily_real)::numeric(10,1)
           ELSE NULL END AS days_full,
      CASE WHEN COALESCE(dt.avg_daily_real, 0) > 0
           THEN ((cs.stock_full + cs.stock_odoo)::numeric / dt.avg_daily_real)::numeric(10,1)
           ELSE NULL END AS days_total,
      -- restock: solo cuenta lo que ya está en FULL, objetivo 45d con target_avg (trend-aware)
      CASE WHEN COALESCE(dt.target_avg, 0) > 0
           THEN GREATEST(0, CEIL(dt.target_avg * 45) - cs.stock_full)::integer
           ELSE 0 END AS restock_suggested,
      CASE
        WHEN COALESCE(dt.avg_prev_14d, 0) = 0 AND COALESCE(dt.avg_14d, 0) > 0 THEN 'up'
        WHEN COALESCE(dt.avg_prev_14d, 0) = 0 THEN 'stable'
        WHEN dt.avg_14d / dt.avg_prev_14d >= 1.15 THEN 'up'
        WHEN dt.avg_14d / dt.avg_prev_14d <= 0.85 THEN 'down'
        ELSE 'stable'
      END AS trend,
      sd.series_14d
    FROM combined_stock cs
    LEFT JOIN demand_target dt ON dt.sku = cs.sku AND dt.cuenta = cs.cuenta
    LEFT JOIN series_data   sd ON sd.sku = cs.sku AND sd.cuenta = cs.cuenta
  )
  SELECT
    c.sku, c.cuenta, c.title, c.size_category, c.logistic_type, c.item_status,
    c.stock_full, c.stock_odoo, c.price,
    c.avg_daily, c.avg_daily_real,
    c.days_full, c.days_total, c.restock_suggested,
    CASE
      WHEN c.stock_full = 0 AND c.stock_odoo = 0 THEN 'stockout'
      WHEN c.avg_daily_real = 0                   THEN 'no_sales'
      WHEN c.days_full IS NULL                    THEN 'no_stock'
      WHEN c.days_full < 14                       THEN 'critical'
      WHEN c.days_full < 30                       THEN 'warn'
      ELSE 'ok'
    END AS signal,
    c.trend,
    c.series_14d
  FROM computed c
  ORDER BY
    CASE
      WHEN c.stock_full = 0 AND c.stock_odoo = 0 THEN 0
      WHEN c.avg_daily_real = 0                   THEN 4
      WHEN c.days_full IS NULL                    THEN 5
      WHEN c.days_full < 14                       THEN 1
      WHEN c.days_full < 30                       THEN 2
      ELSE 3
    END,
    c.days_full ASC NULLS LAST;
$$;
