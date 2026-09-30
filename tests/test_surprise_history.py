import statistics

import pytest

from app import model, surprise_history as sh


def row(date, crude, gasoline=0.0, distillate=0.0):
    return {"release_date": date, "crude_surprise_mb": crude, "gasoline_surprise_mb": gasoline,
            "distillate_surprise_mb": distillate}


# ------------------------------------------------------------------- model

def test_seasonal_weights():
    assert (model.omega_gasoline(1), model.omega_gasoline(5), model.omega_gasoline(9), model.omega_gasoline(10)) \
        == (0.67, 0.80, 0.80, 0.67)
    assert (model.omega_distillate(10), model.omega_distillate(11), model.omega_distillate(2), model.omega_distillate(3)) \
        == (0.50, 0.70, 0.70, 0.50)


def test_tls_reference_week():
    # 23-09-2026 (September): crude +3.569, gasoline -1.786, distillate +0.172
    assert model.tls(3.569, -1.786, 0.172, 9) == pytest.approx(3.569 - 0.80 * 1.786 + 0.50 * 0.172)


def test_history_tls_uses_the_month_of_that_release():
    assert model.history_tls(row("21-01-2026", 0, 0, 1.0)) == pytest.approx(0.70)
    assert model.history_tls(row("22-07-2026", 0, 1.0, 0)) == pytest.approx(0.80)


def test_std_method_is_the_sample_std_dev_of_the_last_twelve_weeks():
    days = [f"{d:02d}-03-2026" for d in range(1, 29)]
    history = [row(d, float(i)) for i, d in enumerate(days)]            # crude only: TLS == crude
    assert model.sigma_forecast(history, method="std") == pytest.approx(statistics.stdev(range(16, 28)))   # last 12
    assert model.sigma_forecast(list(reversed(history)), method="std") == pytest.approx(statistics.stdev(range(16, 28)))


def weekly(*crude):
    return [row(f"{d:02d}-03-2026", c) for d, c in enumerate(crude, start=1)]


def test_mad_method_shrugs_off_one_freak_week_but_std_does_not():
    normal = [1.0, -2.0, 3.0, -1.0, 2.0, -3.0, 1.5, -0.5]
    calm, freak = weekly(*normal), weekly(*normal[:-1], 20.0)
    std_jump = model.sigma_forecast(freak, method="std") / model.sigma_forecast(calm, method="std")
    mad_jump = model.sigma_forecast(freak) / model.sigma_forecast(calm)         # default is "mad"
    assert std_jump > 3 and mad_jump < 1.5


def test_mad_is_1_4826_times_the_median_absolute_deviation():
    history = weekly(1, 2, 3, 4, 5, 6, 7, 8)             # median 4.5, |dev| median 2.0
    assert model.sigma_forecast(history) == pytest.approx(1.4826 * 2.0)


def test_zero_mad_falls_back_to_std_instead_of_dividing_by_zero():
    history = weekly(1, 1, 1, 1, 1, 1, 1, 9)             # MAD 0, but not a flat series
    assert model.sigma_forecast(history) == pytest.approx(statistics.stdev([1, 1, 1, 1, 1, 1, 1, 9]))


def test_unknown_sigma_method_is_refused():
    with pytest.raises(ValueError, match="sigma method"):
        model.sigma_forecast(weekly(*range(8)), method="mean")


def test_sigma_refuses_a_short_history():
    with pytest.raises(ValueError, match="needs 8 weeks of surprise history, have 3"):
        model.sigma_forecast([row("01-04-2026", 1.0), row("08-04-2026", 2.0), row("15-04-2026", 3.0)])


def test_z_score():
    assert model.z_score(3.0, 2.0) == 1.5


# ----------------------------------------------------------------- history

def cal(date, actual, consensus):
    return {"release_date": date, "time": "08:00 PM", "actual": actual, "consensus": consensus, "previous": 0.0}


def test_rows_need_actual_and_consensus_on_all_three_legs():
    rows = sh.rows_from_pages({
        "crude": [cal("23-09-2026", 2.969, -0.7), cal("16-09-2026", -0.5, None), cal("30-09-2026", None, -1.0)],
        "gasoline": [cal("23-09-2026", -1.686, 0.1), cal("16-09-2026", 1.0, 0.0)],
        "distillate": [cal("23-09-2026", -0.428, -0.6), cal("16-09-2026", 0.5, 0.2)],
    })
    assert rows == [{"release_date": "23-09-2026", "crude_surprise_mb": 3.669,
                     "gasoline_surprise_mb": -1.786, "distillate_surprise_mb": 0.172}]


def test_merge_never_overwrites_and_sorts_by_real_date():
    existing = [row("09-09-2026", 1.0), row("23-09-2026", 3.0)]
    merged = sh.merge(existing, [row("23-09-2026", 99.0), row("16-09-2026", 2.0), row("02-10-2026", 4.0)])
    assert [r["release_date"] for r in merged] == ["09-09-2026", "16-09-2026", "23-09-2026", "02-10-2026"]
    assert merged[2]["crude_surprise_mb"] == 3.0          # existing week kept
    assert sh.merge([row("02-10-2026", 4.0)], [row("25-09-2026", 1.0)])[0]["release_date"] == "25-09-2026"


def test_record_week_appends_once(tmp_path):
    path = tmp_path / "history.json"
    consensus = {"crude_consensus_mb": -0.6, "gasoline_consensus_mb": 0.1, "distillate_consensus_mb": -0.6}
    actuals = {"release_date": "23-09-2026", "crude_change_mb": 2.969, "gasoline_change_mb": -1.686,
               "distillate_change_mb": -0.428}
    first = sh.record_week(consensus, actuals, path)
    again = sh.record_week(consensus, actuals, path)
    assert first == again == [{"release_date": "23-09-2026", "crude_surprise_mb": 3.569,
                               "gasoline_surprise_mb": -1.786, "distillate_surprise_mb": 0.172}]
    assert sh.load_history(path) == first


def test_backfill_paces_the_three_sessions_and_fails_loudly():
    class FakeScraper:
        session_gap_s = 60

        def __init__(self, pages):
            self.pages = pages

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def fetch_page(self, slug):
            return self.pages.get(slug)

    good = {"eia-crude-oil-inventories-75": {"calendar_rows": [cal("23-09-2026", 2.969, -0.7)]},
            "weekly-gasoline-inventories-485": {"calendar_rows": [cal("23-09-2026", -1.686, 0.1)]},
            "eia-weekly-distillates-stocks-917": {"calendar_rows": [cal("23-09-2026", -0.428, -0.6)]}}
    sleeps = []
    rows = sh.backfill(lambda site: FakeScraper(good), sleep=sleeps.append)
    assert rows[0]["release_date"] == "23-09-2026" and sleeps == [60, 60]

    blocked = {k: v for k, v in good.items() if not k.startswith("weekly-gasoline")}
    with pytest.raises(RuntimeError, match="no calendar for eia_gasoline"):
        sh.backfill(lambda site: FakeScraper(blocked), sleep=lambda s: None)
