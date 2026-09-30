from datetime import datetime, timedelta

import pytest

from app import ng_recorder as rec
from app.market_data import NEW_YORK
from app.scraper.utils.calendar import to_mb_suffixed
from app.utils import ng_storage

CSV = '''﻿Energy Information Administration
"Working Gas in Underground Storage, Lower 48"
"Released: September 24, 2026 at 10:30 a.m. (eastern time) for the Week Ending September 18, 2026"
"Next Release: October 1, 2026"
Region,Stocks in billion (Bcf),,,,,,,,,,,,Historical Comparisons
East,"815","","","795","",,"20","",,"20",,"","803",,,"1.5",,,"775",,,"5.2",,"",""
Total,"3,351","","","3,298","",,"53","",,"53",,"","3,497",,,"-4.2",,,"3,256",,,"2.9",,"",""
Note: This table is provided for ready processing of weekly volumes estimates.
'''


def test_bcf_suffixes_parse():
    assert to_mb_suffixed("53Bcf") == 53.0 and to_mb_suffixed("-5.00B") == -5.0
    assert to_mb_suffixed("-1.6M") == -1.6 and to_mb_suffixed("250K") == 0.25   # crude unchanged


def test_storage_csv_total_row():
    s = ng_storage.parse_storage_csv(CSV)
    assert (s["release_date"], s["week_ending"]) == ("24-09-2026", "18-09-2026")
    assert (s["total_bcf"], s["net_change_bcf"], s["implied_flow_bcf"]) == (3351, 53, 53)
    assert (s["five_year_avg_bcf"], s["pct_vs_five_year_avg"]) == (3256, 2.9) and s["reclassified"] is False


def test_a_reclassification_is_flagged():
    s = ng_storage.parse_storage_csv(CSV.replace('"53","",,"53"', '"53","",,"38"'))
    assert s["reclassified"] is True


def test_storage_csv_without_a_total_row_raises():
    with pytest.raises(ValueError):
        ng_storage.parse_storage_csv(CSV.replace("Total,", "Other,"))


def test_fetch_storage_returns_none_when_unreachable():
    def boom(*a, **k):
        raise OSError("down")
    assert ng_storage.fetch_storage(get=boom) is None


def bars(interval, start_min=-10, minutes=70, base=3.0, step=0.001):
    """Synthetic bars around the 24-09-2026 print: the open rises `step` a bar."""
    t0 = datetime(2026, 9, 24, 10, 30, tzinfo=NEW_YORK)
    out, i = [], 0
    for m in range(start_min, minutes, interval):
        o = base + step * i
        out.append((t0 + timedelta(minutes=m), o, o + 0.01, o - 0.01, o + step))
        i += 1
    return out


def test_one_minute_path_has_every_offset():
    p = rec.path_from_bars(bars(1), "24-09-2026", 1)
    assert set(p["prices"]) == {"0", "1", "2", "5", "10", "15", "30", "60"}
    assert p["moves"]["60"] > p["moves"]["5"] > 0 and p["interval_min"] == 1


def test_five_minute_path_leaves_one_and_two_minute_empty():
    p = rec.path_from_bars(bars(5), "24-09-2026", 5)
    assert p["prices"]["1"] is None and p["prices"]["2"] is None
    assert p["prices"]["0"] is not None and p["prices"]["5"] is not None and p["prices"]["60"] is not None
    assert p["high_1h"] > p["low_1h"]


def test_a_holiday_release_at_noon_is_timed_from_noon():
    assert rec.print_time("24-09-2026").hour == 10 and rec.print_time("25-11-2026").hour == 12
    assert rec.print_time("13-11-2026").hour == 10 and rec.print_time("13-11-2026").minute == 30   # Veterans Day: Friday, same time
    t0 = datetime(2026, 11, 25, 12, 0, tzinfo=NEW_YORK)
    noon = [(t0 + timedelta(minutes=m), 3.0 + 0.001 * m, 3.01, 2.99, 3.0 + 0.001 * m) for m in range(-10, 70, 5)]
    p = rec.path_from_bars(noon, "25-11-2026", 5)
    assert p["pre_print"] is not None and p["prices"]["60"] is not None


def test_no_pre_print_bar_gives_no_path():
    late = [b for b in bars(5) if b[0] >= datetime(2026, 9, 24, 10, 30, tzinfo=NEW_YORK)]
    assert rec.path_from_bars(late, "24-09-2026", 5) is None


def test_merge_never_erases_recorded_data():
    old = {"actual_bcf": 53, "eia": {"total_bcf": 3351}, "price": {"pre_print": 3.1}}
    new = {"actual_bcf": None, "eia": None, "price": {"pre_print": None, "high_1h": 3.2}, "extra": 1}
    m = rec.merge(old, new)
    assert m["actual_bcf"] == 53 and m["eia"] == {"total_bcf": 3351}
    assert m["price"] == {"pre_print": 3.1, "high_1h": 3.2} and m["extra"] == 1


def row(date, actual, consensus, previous=44.0):
    return {"release_date": date, "time": "08:00 PM", "actual": actual, "consensus": consensus, "previous": previous}


ROWS = {"tradingeconomics": [row("24-09-2026", 53.0, 53.0)],
        "investing": [row("24-09-2026", 53.0, 50.0), row("01-10-2026", None, 63.0, 53.0)]}


def test_both_consensus_panels_and_both_surprises_are_kept():
    r = rec.build_record("24-09-2026", ROWS, ng_storage.parse_storage_csv(CSV), bars(5), 5, "t")
    assert r["actual_bcf"] == 53.0 and r["actual_agrees"] is True
    assert r["consensus_bcf"] == {"tradingeconomics": 53.0, "investing.com": 50.0}
    assert r["surprise_bcf"] == {"tradingeconomics": 0.0, "investing.com": 3.0}
    assert r["eia"]["five_year_avg_bcf"] == 3256 and r["price"]["interval_min"] == 5


def test_an_unreleased_row_records_nothing_and_eia_for_another_week_is_dropped():
    r = rec.build_record("01-10-2026", ROWS, ng_storage.parse_storage_csv(CSV), None, None, "t")
    assert r["actual_bcf"] is None and r["consensus_bcf"] == {} and r["eia"] is None and r["price"] is None


def test_actuals_that_disagree_are_flagged():
    rows = {"tradingeconomics": [row("24-09-2026", 53.0, 53.0)], "investing": [row("24-09-2026", 52.0, 50.0)]}
    assert rec.build_record("24-09-2026", rows, None, None, None, "t")["actual_agrees"] is False


def test_latest_release_ignores_unreleased_rows():
    assert rec.latest_release(ROWS) == "24-09-2026"
    assert rec.latest_release({"investing": [row("01-10-2026", None, 63.0)]}) is None


def test_record_is_idempotent_and_keeps_earlier_data():
    data = {"records": {}}
    rec.record("24-09-2026", ROWS, ng_storage.parse_storage_csv(CSV), bars(5), 5, data, "first")
    rec.record("24-09-2026", ROWS, None, None, None, data, "second")   # a later, poorer run
    r = data["records"]["24-09-2026"]
    assert len(data["records"]) == 1 and r["eia"]["total_bcf"] == 3351 and r["price"] and r["recorded_at"] == "second"


def test_show_reports_the_slope_only_with_three_releases():
    data = {"records": {}}
    for d, actual, cons, move in (("03-09-2026", 30, 30, 0.0), ("10-09-2026", 40, 35, -0.01), ("17-09-2026", 44, 49, 0.01)):
        data["records"][d] = {"actual_bcf": actual, "surprise_bcf": {"investing.com": actual - cons},
                              "price": {"moves": {"5": move, "30": move, "60": move}}}
    lines = rec.show(data)
    assert any("slope at 5 min" in line for line in lines)
    assert rec.show({"records": {}}) == ["nothing recorded yet"]


class FakeScraper:
    def __init__(self, site, rows):
        self.site, self.rows = site, rows

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def fetch_page(self, slug):
        return {"calendar_rows": self.rows.get(self.site), "stats": {}}


def test_main_writes_the_record_and_alerts_only_on_failure(monkeypatch, tmp_path):
    monkeypatch.setattr(rec, "load_dotenv", lambda *a, **k: None)
    monkeypatch.setattr(rec, "NG_RECORD_FILE", tmp_path / "ng.json")
    monkeypatch.setattr(rec, "fetch_storage", lambda: ng_storage.parse_storage_csv(CSV))
    monkeypatch.setattr(rec, "fetch_bars", lambda date, today=None: (bars(5), 5))
    alerts = []
    monkeypatch.setattr(rec, "send_exception", lambda script, exc: alerts.append(script))
    assert rec.main([], make_scraper=lambda site: FakeScraper(site, ROWS)) == 0
    saved = rec.load(tmp_path / "ng.json")["records"]["24-09-2026"]
    assert saved["surprise_bcf"]["investing.com"] == 3.0 and alerts == []

    assert rec.main([], make_scraper=lambda site: FakeScraper(site, {})) == 1 and alerts == ["ng_recorder.py"]


def test_backfill_records_every_released_investing_row(monkeypatch, tmp_path):
    monkeypatch.setattr(rec, "load_dotenv", lambda *a, **k: None)
    monkeypatch.setattr(rec, "NG_RECORD_FILE", tmp_path / "ng.json")
    monkeypatch.setattr(rec, "fetch_storage", lambda: None)
    monkeypatch.setattr(rec, "fetch_bars", lambda date, today=None: (None, None))
    rows = {"investing": [row("17-09-2026", 44.0, 49.0, 40.0), row("24-09-2026", 53.0, 50.0),
                          row("01-10-2026", None, 63.0)]}
    assert rec.main(["--backfill"], make_scraper=lambda site: FakeScraper(site, rows)) == 0
    assert sorted(rec.load(tmp_path / "ng.json")["records"]) == ["17-09-2026", "24-09-2026"]
