-- Add pricing for the Opus models (migration 0003). Rates are USD per 1M tokens,
-- from the Claude API model table; cache-write multipliers as in migration 0002.

INSERT INTO app.pricing
    (pricing_id, provider, model, effective_from, effective_to, currency,
     rates_per_million, modifiers, source_url, checked_at)
VALUES
    ('44444444-4444-4444-4444-444444444444', 'anthropic', 'claude-opus-4-8',
     '2026-10-06T00:00:00Z', NULL, 'USD',
     '{"input": 5.00, "output": 25.00, "cache_read": 0.50, "cache_write_5m": 6.25, "cache_write_1h": 10.00}',
     '{"cache_write_5m_multiplier": 1.25, "cache_write_1h_multiplier": 2.0}',
     'https://platform.claude.com/docs/en/about-claude/pricing', '2026-10-23T00:00:00Z'),

    ('55555555-5555-5555-5555-555555555555', 'anthropic', 'claude-opus-5',
     '2026-10-06T00:00:00Z', NULL, 'USD',
     '{"input": 5.00, "output": 25.00, "cache_read": 0.50, "cache_write_5m": 6.25, "cache_write_1h": 10.00}',
     '{"cache_write_5m_multiplier": 1.25, "cache_write_1h_multiplier": 2.0}',
     'https://platform.claude.com/docs/en/about-claude/pricing', '2026-10-23T00:00:00Z')
ON CONFLICT (provider, model, effective_from) DO NOTHING;
