-- Freshness is a property of the data, not of when the report ran, so it comes from the source.
SELECT CAST(MAX(booked_on) AS STRING) AS watermark
FROM demo.pnl_demo.pnl_fact
