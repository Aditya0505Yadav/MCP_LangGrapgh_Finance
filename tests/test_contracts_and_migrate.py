"""Input validation contracts and the migration SQL splitter."""

from app.db.migrate import _split_sql_statements
from mcp_server.contracts import validate_stock_input


def test_validate_stock_input_ok():
    assert validate_stock_input("AAPL", "2023-01-01", "2023-12-31") is None


def test_validate_stock_input_empty_symbol():
    assert validate_stock_input("", "2023-01-01", "2023-12-31") == "invalid_symbol"


def test_validate_stock_input_bad_dates():
    assert validate_stock_input("AAPL", "not-a-date", "2023-12-31") == "invalid_date_range"
    assert validate_stock_input("AAPL", "2023-12-31", "2023-01-01") == "invalid_date_range"


def test_validate_stock_input_range_too_long():
    assert validate_stock_input("AAPL", "2000-01-01", "2023-12-31") == "invalid_date_range"


def test_split_sql_ignores_comments_and_string_semicolons():
    script = """
    -- a comment with ; semicolon
    CREATE TABLE t (a text);
    INSERT INTO t VALUES ('x;y');  -- trailing comment
    """
    stmts = _split_sql_statements(script)
    assert len(stmts) == 2
    assert stmts[0].startswith("CREATE TABLE")
    assert "'x;y'" in stmts[1]
