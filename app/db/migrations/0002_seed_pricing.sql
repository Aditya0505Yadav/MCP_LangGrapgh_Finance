-- Seed versioned pricing records (migration 0002).
--
-- Rates are USD per 1,000,000 tokens. input/output/cache_read are taken from the
-- Claude API model table (cached 2026-10-06) and the Anthropic pricing page.
-- cache_write_5m / cache_write_1h follow Anthropic's published cache-write
-- multipliers (5-minute TTL = 1.25x base input, 1-hour TTL = 2x base input);
-- this is recorded in `modifiers`. Cost logic reads THESE rows — nothing is
-- hard-coded in Python. Re-verify against the source before trusting dollar
-- figures; an unknown rate yields NULL cost, never zero.

INSERT INTO app.pricing
    (pricing_id, provider, model, effective_from, effective_to, currency,
     rates_per_million, modifiers, source_url, checked_at)
VALUES
    ('11111111-1111-1111-1111-111111111111', 'anthropic', 'claude-opus-5-5',
     '2026-10-06T00:00:00Z', NULL, 'USD',
     '{"input": 4.00, "output": 20.00, "cache_read": 0.20, "cache_write_5m": 5.00, "cache_write_1h": 8.00}',
     '{"cache_write_5m_multiplier": 1.25, "cache_write_1h_multiplier": 2.0, "note": "cache-write rates derived from base input"}',
     'https://platform.claude.com/docs/en/about-claude/pricing', '2026-10-22T00:00:00Z'),

    ('22222222-2222-2222-2222-222222222222', 'anthropic', 'claude-sonnet-5-5',
     '2026-10-06T00:00:00Z', NULL, 'USD',
     '{"input": 2.00, "output": 10.00, "cache_read": 0.20, "cache_write_5m": 2.50, "cache_write_1h": 4.00}',
     '{"cache_write_5m_multiplier": 1.25, "cache_write_1h_multiplier": 2.0, "note": "cache-write rates derived from base input"}',
     'https://platform.claude.com/docs/en/about-claude/pricing', '2026-10-22T00:00:00Z'),

    ('33333333-3333-3333-3333-333333333333', 'anthropic', 'claude-haiku-5-5',
     '2026-10-06T00:00:00Z', NULL, 'USD',
     '{"input": 0.10, "output": 0.50, "cache_read": 0.01, "cache_write_5m": 0.125, "cache_write_1h": 0.20}',
     '{"cache_write_5m_multiplier": 1.25, "cache_write_1h_multiplier": 2.0, "note": "cache-read/write derived; <=100k prompt tier"}',
     'https://platform.claude.com/docs/en/about-claude/pricing', '2026-10-22T00:00:00Z')
ON CONFLICT (provider, model, effective_from) DO NOTHING;
