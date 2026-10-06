"""Parser tests, including against captured real-portal markup."""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

import pytest

from custom_components.monctonwater.api import (
    BilledReading,
    is_login_page,
    parse_account_info,
    parse_billed_readings,
    parse_daily_readings,
    parse_hourly_values,
)
from custom_components.monctonwater.exceptions import MonctonWaterApiError
from custom_components.monctonwater.statistics import (
    ASSUMED_FIRST_PERIOD_DAYS,
    billed_spans,
    spread_days,
)

FIXTURES = Path(__file__).parent / "fixtures"


def test_parse_billed_readings_real_capture():
    html = (FIXTURES / "consumption_table.html").read_text(encoding="utf-8")
    readings = parse_billed_readings(html)
    assert len(readings) == 13
    assert readings[0].read_date == date(2023, 6, 15)
    assert readings[-1].read_date == date(2026, 6, 15)
    # Sorted ascending regardless of the table's newest-first order.
    assert [r.read_date for r in readings] == sorted(
        r.read_date for r in readings
    )
    # Neutralized capture: every value was the captured one + 1.0.
    assert readings[-1].consumption_m3 == pytest.approx(69.0)
    assert readings[0].consumption_m3 == pytest.approx(80.0)


def test_parse_daily_readings_real_capture():
    html = (FIXTURES / "smart_meter_arrays.html").read_text(encoding="utf-8")
    readings = parse_daily_readings(html)
    assert len(readings) == 30
    assert readings[0].day == date(2026, 8, 27)
    assert readings[-1].day == date(2026, 9, 25)
    # Neutralized capture: every value was the captured one + 0.001.
    assert readings[0].consumption_m3 == pytest.approx(0.780)
    assert readings[-1].consumption_m3 == pytest.approx(0.814)
    assert all(r.consumption_m3 > 0 for r in readings)


def test_parse_daily_readings_empty_page():
    assert parse_daily_readings("<html><body>no data</body></html>") == []


def test_parse_daily_readings_keeps_alignment_across_gaps():
    """A non-numeric entry drops only its own day, never shifts later ones."""
    html = (
        'var aTouDate = ["2026-09-01","2026-09-02","2026-09-03"];\n'
        'series: [{ type: "column", name: "Usage", data: [0.5,null,0.7,] }]'
    )
    readings = parse_daily_readings(html)
    assert [(r.day, r.consumption_m3) for r in readings] == [
        (date(2026, 9, 1), 0.5),
        (date(2026, 9, 3), 0.7),
    ]


def test_parse_hourly_values_keeps_hour_positions():
    """A gap reads as 0 for its hour; later hours keep their slots."""
    data = ",".join(["0.01"] * 5 + ["null"] + ["0.02"] * 18)
    html = f'series: [{{ type: "column", name: "Usage", data: [{data}] }}]'
    values = parse_hourly_values(html)
    assert len(values) == 24
    assert values[5] == 0.0
    assert values[6] == pytest.approx(0.02)
    assert parse_hourly_values('name: "Usage", data: [null,null]') == []


def test_parse_hourly_values_real_capture():
    html = (FIXTURES / "smart_meter_arrays.html").read_text(encoding="utf-8")
    # The fixture holds the daily variant: one value per date.
    values = parse_hourly_values(html)
    assert len(values) == 30


def test_is_login_page():
    assert is_login_page('<form id="login-form"></form>')
    assert not is_login_page("<html><body>dashboard</body></html>")


def test_parse_account_info_requires_marker():
    with pytest.raises(MonctonWaterApiError):
        parse_account_info("<html></html>")


def test_parse_account_info_fallback_link():
    """A fresh-login page without the header still yields the account."""
    html = (
        '<html><body><a href="/app/capricorn?para=selectAccount&userAction=select'
        '&inAccountNumber=123456-789012&inMeterID=123456&meterType=Water">x</a>'
        "</body></html>"
    )
    account = parse_account_info(html)
    assert account.account_number == "123456-789012"
    assert account.meter_id == "123456"


def test_build_ssl_context_loads_bundled_intermediate():
    """The context builds and trusts the portal's intermediate CA."""
    from custom_components.monctonwater.api import build_ssl_context

    context = build_ssl_context()
    import ssl

    assert context.verify_mode == ssl.CERT_REQUIRED
    assert context.check_hostname is True
    stats = context.cert_store_stats()
    # The intermediate adds at least one cert on top of the default store.
    assert stats["x509_ca"] >= 1


def test_bundled_intermediate_is_not_about_to_expire():
    """Fail well before the bundled intermediate lapses (see api.py)."""
    from datetime import datetime, timezone

    from cryptography import x509

    from custom_components.monctonwater.api import _CERTS_DIR, _EXTRA_CA_BUNDLES

    for name in _EXTRA_CA_BUNDLES:
        cert = x509.load_pem_x509_certificate((_CERTS_DIR / name).read_bytes())
        expires = cert.not_valid_after_utc
        assert expires - datetime.now(timezone.utc) > timedelta(days=90), (
            f"{name} expires {expires:%Y-%m-%d}; bundle the portal's current issuer"
        )


def test_parse_hourly_csv():
    """Rows without an ISO date are skipped; 24 hourly values per day."""
    from custom_components.monctonwater.api import parse_hourly_csv

    values = [f"{i / 100:.5f}" for i in range(24)]
    csv_text = (
        '"Reading Date","1 am CFF Usage",...,"Total CFF Usage"\n'
        "\n"
        f'"2026-09-24",{",".join(values)},"0.24000"\n'
        f'"2026-09-25",{",".join(values)},"0.24000"\n'
        "\n"
    )
    readings = parse_hourly_csv(csv_text)
    assert [r.day for r in readings] == [date(2026, 9, 24), date(2026, 9, 25)]
    assert readings[0].values[0] == pytest.approx(0.0)
    assert readings[0].values[23] == pytest.approx(0.23)
    assert sum(readings[0].values) == pytest.approx(2.76)


def test_billed_spans_chain_periods():
    readings = [
        BilledReading(read_date=date(2026, 3, 15), consumption_m3=70.0),
        BilledReading(read_date=date(2026, 6, 15), consumption_m3=68.0),
        BilledReading(read_date=date(2026, 9, 16), consumption_m3=65.0),
    ]
    spans = billed_spans(readings)
    # The earliest reading gets an assumed period ending at its read date.
    assert spans[0][0] == date(2026, 3, 15) - timedelta(
        days=ASSUMED_FIRST_PERIOD_DAYS - 1
    )
    assert spans[0][1] == date(2026, 3, 15)
    # Later periods start the day after the previous read.
    assert spans[1][0] == date(2026, 3, 16)
    assert spans[1][1] == date(2026, 6, 15)
    assert spans[2][0] == date(2026, 6, 16)
    # Spans are contiguous and non-overlapping.
    for (_, end_a, _), (start_b, _, _) in zip(spans, spans[1:]):
        assert start_b == end_a + timedelta(days=1)


def test_spread_days_covers_spans_evenly():
    spans = billed_spans(
        [
            BilledReading(read_date=date(2026, 3, 15), consumption_m3=70.0),
            BilledReading(read_date=date(2026, 6, 15), consumption_m3=92.0),
        ]
    )
    by_day = dict(spread_days(spans, date(2026, 3, 10), date(2026, 3, 20)))
    assert min(by_day) == date(2026, 3, 10)
    assert max(by_day) == date(2026, 3, 20)
    # 70 m3 over the assumed 91-day first period; 92 m3 over Mar 16-Jun 15.
    assert by_day[date(2026, 3, 15)] == pytest.approx(70.0 / 91)
    assert by_day[date(2026, 3, 16)] == pytest.approx(92.0 / 92)
    # Whole periods add back up to their bills; days no span covers are skipped.
    year = spread_days(spans, date(2025, 1, 1), date(2026, 12, 31))
    assert sum(m3 for _, m3 in year) == pytest.approx(162.0)
    assert len(year) == 91 + 92
