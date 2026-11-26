from datetime import date

import pytest

from tsm300.patient import Patient, parse_birth_date, parse_sex


@pytest.mark.parametrize("text", ["1980-01-31", "1980.1.31", "1980/01/31", "19800131", " 1980-01-31 "])
def test_birth_date_is_read_in_the_usual_spellings(text):
    assert parse_birth_date(text) == date(1980, 1, 31)


def test_blank_birth_date_is_none():
    assert parse_birth_date("  ") is None


@pytest.mark.parametrize("text, reason", [
    ("800131", "읽을 수 없습니다"),
    ("홍길동", "읽을 수 없습니다"),
    ("1980-02-30", "없는 날짜"),
    ("1880-01-31", "생년월일로 볼 수 없는"),
    ("2999-01-31", "생년월일로 볼 수 없는"),
])
def test_unreadable_birth_dates_are_rejected(text, reason):
    with pytest.raises(ValueError, match=reason):
        parse_birth_date(text)


def test_sex_accepts_korean_and_english_spellings():
    assert [parse_sex(text) for text in ("남", "M", "male", "여", "f", "")] == ["남", "남", "남", "여", "여", ""]
    with pytest.raises(ValueError, match="성별"):
        parse_sex("x")


def test_age_counts_completed_years():
    patient = Patient(birth_date=date(1980, 11, 27))
    assert patient.age_on(date(2026, 11, 26)) == 45
    assert patient.age_on(date(2026, 11, 27)) == 46
    assert Patient().age_on(date(2026, 11, 26)) is None


def test_file_tag_is_the_chart_number_without_characters_windows_forbids():
    assert Patient("00512", "홍길동").file_tag() == "00512"
    assert Patient("12 34/5", "홍길동").file_tag() == "12345"
    assert Patient(name="홍길동").file_tag() == ""


def test_empty_means_no_detail_at_all():
    assert Patient().is_empty
    assert not Patient(sex="여").is_empty
