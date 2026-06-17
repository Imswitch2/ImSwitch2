import csv
import io
import sys
from pathlib import Path

# Direct import to avoid napari dependency chain through view.__init__
_view_path = Path(__file__).parent.parent / "view"
sys.path.insert(0, str(_view_path))
try:
    from ResultsTableWidget import merge_columns, format_table_value, records_to_csv
finally:
    sys.path.pop(0)


def test_merge_columns_preserves_order_and_deduplicates():
    existing = ["a", "b", "c"]
    incoming = ["b", "d", "c", "e"]
    result = merge_columns(existing, incoming)
    assert result == ["a", "b", "c", "d", "e"]


def test_merge_columns_returns_new_list():
    existing = ["a", "b"]
    incoming = ["c"]
    result = merge_columns(existing, incoming)
    assert result is not existing
    assert existing == ["a", "b"]


def test_merge_columns_stringifies_non_str():
    existing = ["a", "b"]
    incoming = [1, 2.5, "c"]
    result = merge_columns(existing, incoming)
    assert result == ["a", "b", "1", "2.5", "c"]


def test_merge_columns_empty_existing():
    result = merge_columns([], ["a", "b", "c"])
    assert result == ["a", "b", "c"]


def test_merge_columns_empty_incoming():
    result = merge_columns(["a", "b"], [])
    assert result == ["a", "b"]


def test_format_table_value_none_to_empty():
    assert format_table_value(None) == ""


def test_format_table_value_nan_to_empty():
    assert format_table_value(float("nan")) == ""


def test_format_table_value_float_formatting():
    assert format_table_value(1.23456789) == "1.23457"
    assert format_table_value(0.0) == "0"
    assert format_table_value(1234567.89) == "1.23457e+06"


def test_format_table_value_string_passthrough():
    assert format_table_value("x") == "x"
    assert format_table_value("hello world") == "hello world"


def test_format_table_value_int_to_string():
    assert format_table_value(5) == "5"
    assert format_table_value(0) == "0"
    assert format_table_value(-42) == "-42"


def test_format_table_value_empty_string():
    assert format_table_value("") == ""


def test_records_to_csv_basic():
    columns = ["name", "age", "city"]
    records = [
        {"name": "Alice", "age": 30, "city": "NYC"},
        {"name": "Bob", "age": 25, "city": "LA"},
    ]
    result = records_to_csv(columns, records)
    
    lines = result.strip().split("\n")
    assert len(lines) == 3
    assert lines[0] == "name,age,city"
    assert lines[1] == "Alice,30,NYC"
    assert lines[2] == "Bob,25,LA"


def test_records_to_csv_missing_columns():
    columns = ["a", "b", "c"]
    records = [
        {"a": 1, "c": 3},
        {"b": 2},
    ]
    result = records_to_csv(columns, records)
    
    reader = csv.reader(io.StringIO(result))
    rows = list(reader)
    assert rows[0] == ["a", "b", "c"]
    assert rows[1] == ["1", "", "3"]
    assert rows[2] == ["", "2", ""]


def test_records_to_csv_preserves_row_order():
    columns = ["id"]
    records = [{"id": i} for i in range(5)]
    result = records_to_csv(columns, records)
    
    reader = csv.reader(io.StringIO(result))
    rows = list(reader)
    assert rows[0] == ["id"]
    assert [row[0] for row in rows[1:]] == ["0", "1", "2", "3", "4"]


def test_records_to_csv_formats_values():
    columns = ["val"]
    records = [
        {"val": None},
        {"val": float("nan")},
        {"val": 1.23456789},
        {"val": "text"},
    ]
    result = records_to_csv(columns, records)
    
    reader = csv.reader(io.StringIO(result))
    rows = list(reader)
    assert rows[0] == ["val"]
    assert rows[1] == [""]
    assert rows[2] == [""]
    assert rows[3] == ["1.23457"]
    assert rows[4] == ["text"]


def test_records_to_csv_empty_records():
    result = records_to_csv(["a", "b"], [])
    assert result.strip() == "a,b"


def test_records_to_csv_empty_columns():
    result = records_to_csv([], [{"a": 1}])
    reader = csv.reader(io.StringIO(result))
    rows = list(reader)
    assert len(rows) == 2
    assert rows[0] == []
    assert rows[1] == []
