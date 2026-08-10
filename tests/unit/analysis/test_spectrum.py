"""SpectrumFrame・Hann窓FFT・対数band集約・dB変換・平滑化の数値検証。"""

from typing import cast

import numpy as np
import pytest
from numpy.typing import NDArray

from sdp.core.analysis.spectrum import (
    FFT_SIZE,
    SPECTRUM_ATTACK,
    SPECTRUM_BAND_COUNT,
    SPECTRUM_DB_FLOOR,
    SPECTRUM_FPS,
    SPECTRUM_MAX_HZ,
    SPECTRUM_MIN_HZ,
    SPECTRUM_RELEASE,
    SPECTRUM_TIMER_INTERVAL_MS,
    FrequencyAnalysisFrame,
    SpectrumFrame,
    SpectrumProcessor,
    compute_frequency_analysis,
    compute_spectrum,
    effective_max_hz,
    empty_spectrum_frame,
    fit_fft_input,
)

SAMPLE_RATE = 48_000


def sine(
    frequency: float, sample_rate: int = SAMPLE_RATE, size: int = FFT_SIZE
) -> NDArray[np.float32]:
    """0dBFSの正弦波。"""
    t = np.arange(size, dtype=np.float64) / sample_rate
    return np.sin(2.0 * np.pi * frequency * t).astype(np.float32)


def peak_frequency(frame: SpectrumFrame) -> float:
    return float(frame.frequencies_hz[int(np.argmax(frame.levels_db))])


# -- 既定値 -----------------------------------------------------------------


def test_default_constants_are_centralized() -> None:
    """FFT設定は一か所の定数として保持する。"""
    assert FFT_SIZE == 4_096
    assert SPECTRUM_BAND_COUNT == 96
    assert SPECTRUM_MIN_HZ == 30.0
    assert SPECTRUM_MAX_HZ == 20_000.0
    assert SPECTRUM_DB_FLOOR == -90.0
    assert SPECTRUM_FPS == 30
    assert SPECTRUM_TIMER_INTERVAL_MS == 33
    assert 0.0 < SPECTRUM_RELEASE < SPECTRUM_ATTACK <= 1.0


# -- SpectrumFrame ----------------------------------------------------------


def test_frame_arrays_are_copied_and_read_only() -> None:
    """入力配列と共有せず、要素も書き換えられない。"""
    frequencies = np.array([100.0, 200.0], dtype=np.float32)
    levels = np.array([-10.0, -20.0], dtype=np.float32)

    frame = SpectrumFrame(frequencies_hz=frequencies, levels_db=levels)
    frequencies[0] = 999.0

    assert frame.frequencies_hz.tolist() == [100.0, 200.0]
    assert not frame.frequencies_hz.flags.writeable
    assert not frame.levels_db.flags.writeable
    with pytest.raises(ValueError):
        frame.levels_db[0] = 0.0


def test_frame_rejects_mismatched_shapes() -> None:
    """shape不一致は受け付けない。"""
    with pytest.raises(ValueError, match="shape"):
        SpectrumFrame(
            frequencies_hz=np.array([1.0, 2.0], dtype=np.float32),
            levels_db=np.array([-1.0], dtype=np.float32),
        )


@pytest.mark.parametrize("dtype", ["float64", "int32"])
def test_frame_rejects_non_float32(dtype: str) -> None:
    """dtypeはfloat32に限る。"""
    values = np.zeros(1, dtype=np.dtype(dtype))
    with pytest.raises(TypeError, match="float32"):
        SpectrumFrame(
            frequencies_hz=values,  # pyright: ignore[reportArgumentType]
            levels_db=values,  # pyright: ignore[reportArgumentType]
        )


def test_frame_rejects_multi_dimensional_arrays() -> None:
    """1次元だけを受け付ける。"""
    with pytest.raises(ValueError, match="1次元"):
        SpectrumFrame(
            frequencies_hz=np.zeros((2, 2), dtype=np.float32),
            levels_db=np.zeros((2, 2), dtype=np.float32),
        )


def test_frame_rejects_unsorted_or_negative_frequencies() -> None:
    """周波数は非負かつ昇順である必要がある。"""
    with pytest.raises(ValueError, match="昇順"):
        SpectrumFrame(
            frequencies_hz=np.array([200.0, 100.0], dtype=np.float32),
            levels_db=np.array([-1.0, -2.0], dtype=np.float32),
        )
    with pytest.raises(ValueError, match="負"):
        SpectrumFrame(
            frequencies_hz=np.array([-1.0, 100.0], dtype=np.float32),
            levels_db=np.array([-1.0, -2.0], dtype=np.float32),
        )


def test_frame_rejects_non_finite_or_positive_levels() -> None:
    """levelは有限かつ0dB以下。"""
    with pytest.raises(ValueError, match="NaN"):
        SpectrumFrame(
            frequencies_hz=np.array([100.0], dtype=np.float32),
            levels_db=np.array([np.nan], dtype=np.float32),
        )
    with pytest.raises(ValueError, match="0dB"):
        SpectrumFrame(
            frequencies_hz=np.array([100.0], dtype=np.float32),
            levels_db=np.array([1.0], dtype=np.float32),
        )


def test_empty_frame_is_valid() -> None:
    """空フレームは有効な値として扱える。"""
    frame = empty_spectrum_frame()

    assert frame.band_count == 0
    assert frame.frequencies_hz.dtype == np.dtype(np.float32)


# -- FFT 入力 ---------------------------------------------------------------


def test_fit_pads_short_input_on_the_left() -> None:
    """不足分は左を0で埋める（起動直後もshapeが安定する）。"""
    fitted = fit_fft_input(np.ones(3, dtype=np.float32), 8)

    assert fitted.tolist() == [0.0, 0.0, 0.0, 0.0, 0.0, 1.0, 1.0, 1.0]


def test_fit_uses_the_latest_samples_when_input_is_longer() -> None:
    """FFT_SIZE超過時は最新サンプルを使う。"""
    fitted = fit_fft_input(np.arange(10, dtype=np.float32), 4)

    assert fitted.tolist() == [6.0, 7.0, 8.0, 9.0]


def test_fit_of_empty_input_is_all_zero() -> None:
    """空入力は無音として扱う。"""
    assert fit_fft_input(np.empty(0, dtype=np.float32), 4).tolist() == [0.0] * 4


def test_compute_does_not_modify_the_input_array() -> None:
    """入力配列を書き換えない（DC除去・窓掛けはコピー上で行う）。"""
    samples = sine(1_000.0)
    original = samples.copy()

    compute_spectrum(samples, SAMPLE_RATE)

    assert np.array_equal(samples, original)


def test_compute_left_pads_a_short_snapshot() -> None:
    """短いsnapshotでも既定のband数を返す。"""
    frame = compute_spectrum(sine(1_000.0, size=512), SAMPLE_RATE)

    assert frame.band_count == SPECTRUM_BAND_COUNT


# -- 正弦波のピーク ---------------------------------------------------------


@pytest.mark.parametrize("frequency", [100.0, 1_000.0, 10_000.0])
def test_sine_peak_band_is_near_the_input_frequency(frequency: float) -> None:
    """ピークbandが入力周波数の近傍（band間隔の許容幅内）にある。"""
    frame = compute_spectrum(sine(frequency), SAMPLE_RATE)

    # 96band対数軸の1band幅は約7%。隣接band程度のずれを許容する。
    assert peak_frequency(frame) == pytest.approx(frequency, rel=0.10)


@pytest.mark.parametrize("frequency", [100.0, 1_000.0, 10_000.0])
def test_full_scale_sine_reaches_about_zero_db(frequency: float) -> None:
    """窓補正後、0dBFSの正弦波が0dB付近になる（-6dBや+6dBへずれない）。"""
    frame = compute_spectrum(sine(frequency), SAMPLE_RATE)

    assert float(frame.levels_db.max()) == pytest.approx(0.0, abs=2.0)


def test_half_scale_sine_is_about_minus_six_db() -> None:
    """振幅0.5の正弦波は約-6dBになる。"""
    frame = compute_spectrum((sine(1_000.0) * 0.5).astype(np.float32), SAMPLE_RATE)

    assert float(frame.levels_db.max()) == pytest.approx(-6.0, abs=2.0)


def test_peak_band_is_the_maximum_of_the_frame() -> None:
    """ピークを含むbandがフレーム内で最大になる。"""
    frame = compute_spectrum(sine(1_000.0), SAMPLE_RATE)
    peak_index = int(np.argmax(frame.levels_db))

    assert frame.frequencies_hz[peak_index] == pytest.approx(1_000.0, rel=0.10)
    assert float(frame.levels_db[peak_index]) == float(frame.levels_db.max())


def test_hann_window_suppresses_spectral_leakage() -> None:
    """Hann窓により、ピークから離れたbandのレベルが十分に下がる。"""
    frame = compute_spectrum(sine(1_000.0), SAMPLE_RATE)
    peak_index = int(np.argmax(frame.levels_db))
    far = np.concatenate(
        (frame.levels_db[: max(0, peak_index - 8)], frame.levels_db[peak_index + 8 :])
    )

    assert float(far.max()) < float(frame.levels_db[peak_index]) - 30.0


# -- 無音・DC・floor -------------------------------------------------------


def test_silence_falls_to_the_db_floor() -> None:
    """無音は全bandがfloorになる。"""
    frame = compute_spectrum(np.zeros(FFT_SIZE, dtype=np.float32), SAMPLE_RATE)

    assert frame.band_count == SPECTRUM_BAND_COUNT
    assert np.all(frame.levels_db == SPECTRUM_DB_FLOOR)


def test_dc_offset_is_removed_before_the_fft() -> None:
    """直流成分は除去され、表示帯域を持ち上げない。"""
    frame = compute_spectrum(np.full(FFT_SIZE, 0.8, dtype=np.float32), SAMPLE_RATE)

    assert np.all(frame.levels_db == SPECTRUM_DB_FLOOR)


def test_levels_are_clamped_between_floor_and_zero_db() -> None:
    """clippingした過大入力でも0dBを超えず、floorを下回らない。"""
    loud = np.sign(sine(1_000.0)).astype(np.float32)

    frame = compute_spectrum(loud, SAMPLE_RATE)

    assert float(frame.levels_db.max()) <= 0.0
    assert float(frame.levels_db.min()) >= SPECTRUM_DB_FLOOR
    assert np.all(np.isfinite(frame.levels_db))


def test_epsilon_prevents_log_of_zero() -> None:
    """真の無音でもlog(0)による-infやNaNが現れない。"""
    frame = compute_spectrum(np.zeros(FFT_SIZE, dtype=np.float32), SAMPLE_RATE)

    assert np.all(np.isfinite(frame.levels_db))


def test_custom_db_floor_is_respected() -> None:
    """floorは呼び出し側から変更できる。"""
    frame = compute_spectrum(np.zeros(FFT_SIZE, dtype=np.float32), SAMPLE_RATE, db_floor=-60.0)

    assert np.all(frame.levels_db == -60.0)


# -- 周波数軸と band ------------------------------------------------------


def test_band_count_and_ordering() -> None:
    """既定は96bandで、周波数は昇順・範囲内。"""
    frame = compute_spectrum(sine(1_000.0), SAMPLE_RATE)

    assert frame.band_count == SPECTRUM_BAND_COUNT
    assert frame.frequencies_hz.shape == frame.levels_db.shape
    assert np.all(np.diff(frame.frequencies_hz) > 0.0)
    assert float(frame.frequencies_hz[0]) >= SPECTRUM_MIN_HZ
    assert float(frame.frequencies_hz[-1]) <= SPECTRUM_MAX_HZ


def test_band_edges_are_logarithmic() -> None:
    """band幅は対数軸で一定比になる（線形binをそのまま並べない）。"""
    frame = compute_spectrum(sine(1_000.0), SAMPLE_RATE)
    ratios = frame.frequencies_hz[1:] / frame.frequencies_hz[:-1]

    assert float(ratios.std()) < 1e-3
    assert float(ratios.mean()) == pytest.approx(
        (SPECTRUM_MAX_HZ / SPECTRUM_MIN_HZ) ** (1.0 / SPECTRUM_BAND_COUNT), rel=1e-3
    )


def test_content_below_the_minimum_frequency_is_excluded() -> None:
    """30Hz未満の成分は表示帯域を持ち上げない。"""
    frame = compute_spectrum(sine(10.0), SAMPLE_RATE)

    assert float(frame.levels_db.max()) < -20.0


def test_content_above_the_maximum_frequency_is_excluded() -> None:
    """20kHz超の成分は表示帯域を持ち上げない。"""
    frame = compute_spectrum(sine(23_000.0), SAMPLE_RATE)

    assert float(frame.levels_db.max()) < -20.0


def test_effective_max_is_limited_to_nyquist() -> None:
    """表示上限はNyquist以下へ制限する。"""
    assert effective_max_hz(48_000) == SPECTRUM_MAX_HZ
    assert effective_max_hz(22_050) == 11_025.0


def test_low_sample_rate_limits_the_top_band() -> None:
    """低sample rateでは最上bandがNyquist以下になる。"""
    frame = compute_spectrum(sine(1_000.0, sample_rate=8_000), 8_000)

    assert frame.band_count == SPECTRUM_BAND_COUNT
    assert float(frame.frequencies_hz[-1]) < 4_000.0


def test_sample_rate_change_moves_the_frequency_axis() -> None:
    """sample rateが変わると周波数軸も変わる。"""
    wide = compute_spectrum(sine(1_000.0), 48_000)
    narrow = compute_spectrum(sine(1_000.0, sample_rate=16_000), 16_000)

    assert float(narrow.frequencies_hz[-1]) < float(wide.frequencies_hz[-1])
    assert peak_frequency(narrow) == pytest.approx(1_000.0, rel=0.10)


def test_sample_rate_below_the_minimum_band_returns_an_empty_frame() -> None:
    """有効帯域がmin_hz以下なら band を捏造せず空フレームを返す。"""
    frame = compute_spectrum(np.zeros(64, dtype=np.float32), 40)

    assert frame.band_count == 0


def test_bands_without_bins_are_interpolated_not_replicated() -> None:
    """binより狭い低域bandがピーク値の複製で広がらない。"""
    frame = compute_spectrum(sine(1_000.0), SAMPLE_RATE)
    peak = float(frame.levels_db.max())
    # 4096点/48kHzでは分解能約11.7Hz。30～120Hz付近はbinを持たないbandを含む。
    low = frame.levels_db[frame.frequencies_hz < 120.0]

    assert low.size > 0
    assert float(low.max()) < peak - 30.0


def test_bands_without_bins_stay_finite_and_in_range() -> None:
    """無bin bandでも有限・範囲内の値になる。"""
    frame = compute_spectrum(sine(50.0), SAMPLE_RATE)

    assert np.all(np.isfinite(frame.levels_db))
    assert float(frame.levels_db.min()) >= SPECTRUM_DB_FLOOR
    assert float(frame.levels_db.max()) <= 0.0


# -- 引数検証 ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("kwargs", "sample_rate"),
    [
        ({}, 0),
        ({"band_count": 0}, SAMPLE_RATE),
        ({"min_hz": 0.0}, SAMPLE_RATE),
        ({"db_floor": 0.0}, SAMPLE_RATE),
    ],
)
def test_compute_validates_arguments(kwargs: dict[str, object], sample_rate: int) -> None:
    """不正な設定は暗黙に補正せず例外にする。"""
    with pytest.raises(ValueError):
        compute_spectrum(
            np.zeros(FFT_SIZE, dtype=np.float32),
            sample_rate,
            **kwargs,  # pyright: ignore[reportArgumentType]
        )


def test_fit_validates_arguments() -> None:
    """FFT長と次元を検証する。"""
    with pytest.raises(ValueError, match="fft_size"):
        fit_fft_input(np.zeros(4, dtype=np.float32), 0)
    with pytest.raises(ValueError, match="1次元"):
        fit_fft_input(np.zeros((2, 2), dtype=np.float32), 4)


# -- 4096点/96band の処理 --------------------------------------------------


def test_processes_4096_samples_into_96_bands() -> None:
    """既定設定で4096点入力から96band出力を得る。"""
    frame = compute_spectrum(sine(1_000.0), SAMPLE_RATE)

    assert frame.levels_db.shape == (96,)
    assert frame.frequencies_hz.shape == (96,)


# -- 平滑化 -----------------------------------------------------------------


def test_processor_first_frame_is_not_smoothed() -> None:
    """最初のフレームは前回値がないため生の値を返す。"""
    processor = SpectrumProcessor()
    samples = sine(1_000.0)

    smoothed = processor.process(samples, SAMPLE_RATE)
    raw = compute_spectrum(samples, SAMPLE_RATE)

    assert np.allclose(smoothed.levels_db, raw.levels_db, atol=1e-5)


def test_attack_rises_partway_toward_the_new_level() -> None:
    """上昇時はattack係数の分だけ近づく（即時に到達しない）。"""
    processor = SpectrumProcessor(attack=0.5, release=0.1)
    processor.process(np.zeros(FFT_SIZE, dtype=np.float32), SAMPLE_RATE)

    rising = processor.process(sine(1_000.0), SAMPLE_RATE)
    peak = float(rising.levels_db.max())
    target = float(compute_spectrum(sine(1_000.0), SAMPLE_RATE).levels_db.max())

    assert SPECTRUM_DB_FLOOR < peak < target
    assert peak == pytest.approx(SPECTRUM_DB_FLOOR + 0.5 * (target - SPECTRUM_DB_FLOOR), abs=0.5)


def test_release_decays_more_slowly_than_attack_rises() -> None:
    """減衰はattackより緩やかで、無音が続けばfloorへ近づく。"""
    processor = SpectrumProcessor()
    processor.process(sine(1_000.0), SAMPLE_RATE)
    silence = np.zeros(FFT_SIZE, dtype=np.float32)

    first = float(processor.process(silence, SAMPLE_RATE).levels_db.max())
    last = first
    for _ in range(60):
        last = float(processor.process(silence, SAMPLE_RATE).levels_db.max())

    assert first > SPECTRUM_DB_FLOOR + 10.0
    assert last == pytest.approx(SPECTRUM_DB_FLOOR, abs=1.0)


def test_reset_discards_the_smoothing_history() -> None:
    """resetすると次フレームは生の値から始まる。"""
    processor = SpectrumProcessor()
    processor.process(sine(1_000.0), SAMPLE_RATE)

    processor.reset()
    assert processor.sample_rate is None
    after = processor.process(np.zeros(FFT_SIZE, dtype=np.float32), SAMPLE_RATE)

    assert np.all(after.levels_db == SPECTRUM_DB_FLOOR)


def test_processor_resets_when_the_sample_rate_changes() -> None:
    """sample rate変更で旧formatの平滑化状態を持ち越さない。"""
    processor = SpectrumProcessor()
    processor.process(sine(1_000.0), 48_000)

    switched = processor.process(np.zeros(FFT_SIZE, dtype=np.float32), 16_000)

    assert processor.sample_rate == 16_000
    assert np.all(switched.levels_db == SPECTRUM_DB_FLOOR)


def test_processor_returns_an_empty_frame_for_an_unusable_sample_rate() -> None:
    """有効帯域が無い場合は空フレームを返し、履歴も残さない。"""
    processor = SpectrumProcessor()
    processor.process(sine(1_000.0), 48_000)

    frame = processor.process(np.zeros(64, dtype=np.float32), 40)

    assert frame.band_count == 0


def test_processor_output_stays_within_the_db_range() -> None:
    """平滑化後もfloor～0dBの範囲を守る。"""
    processor = SpectrumProcessor()
    loud = np.sign(sine(1_000.0)).astype(np.float32)
    frame = processor.process(loud, SAMPLE_RATE)

    for _ in range(10):
        frame = processor.process(loud, SAMPLE_RATE)

    assert float(frame.levels_db.max()) <= 0.0
    assert float(frame.levels_db.min()) >= SPECTRUM_DB_FLOOR


@pytest.mark.parametrize("value", [0.0, -0.1, 1.1, float("nan"), float("inf"), True])
def test_processor_validates_smoothing_coefficients(value: float) -> None:
    """係数は0より大きく1以下に限る。"""
    with pytest.raises(ValueError):
        SpectrumProcessor(attack=value)
    with pytest.raises(ValueError):
        SpectrumProcessor(release=value)


def test_processor_does_not_modify_the_input_array() -> None:
    """平滑化でも入力配列を書き換えない。"""
    processor = SpectrumProcessor()
    samples = sine(1_000.0)
    original = samples.copy()

    processor.process(samples, SAMPLE_RATE)
    processor.process(samples, SAMPLE_RATE)

    assert np.array_equal(samples, original)


# -- FFT結果の共有（FrequencyAnalysisFrame）--------------------------------


def test_shared_analysis_gives_the_same_spectrum_as_a_standalone_call() -> None:
    """共有したFFT結果から作ったスペクトラムは、単独計算と一致する。"""
    samples = sine(1_000.0)
    analysis = compute_frequency_analysis(samples, SAMPLE_RATE)

    shared = compute_spectrum(samples, SAMPLE_RATE, analysis=analysis)
    standalone = compute_spectrum(samples, SAMPLE_RATE)

    assert np.array_equal(shared.levels_db, standalone.levels_db)
    assert np.array_equal(shared.frequencies_hz, standalone.frequencies_hz)


def test_shared_analysis_runs_the_fft_only_once(monkeypatch: pytest.MonkeyPatch) -> None:
    """共有結果を渡した呼び出しはrFFTを実行しない。"""
    samples = sine(1_000.0)
    analysis = compute_frequency_analysis(samples, SAMPLE_RATE)
    calls: list[int] = []
    original = np.fft.rfft

    def counting(*args: object, **kwargs: object) -> object:
        calls.append(1)
        return cast("object", original(*args, **kwargs))  # pyright: ignore[reportCallIssue, reportArgumentType]

    monkeypatch.setattr(np.fft, "rfft", counting)
    compute_spectrum(samples, SAMPLE_RATE, analysis=analysis)

    assert calls == []


def test_shared_analysis_is_not_consumed_by_the_first_reader() -> None:
    """共有結果は読み取り側で書き換わらず、2度目以降も同じ値を返す。"""
    samples = sine(1_000.0)
    analysis = compute_frequency_analysis(samples, SAMPLE_RATE)
    magnitudes = analysis.magnitudes.copy()

    compute_spectrum(samples, SAMPLE_RATE, analysis=analysis)

    assert np.array_equal(analysis.magnitudes, magnitudes)
    assert not analysis.magnitudes.flags.writeable


def test_shared_analysis_with_a_different_sample_rate_is_rejected() -> None:
    """古い世代のFFT結果を黙って使わない。"""
    analysis = compute_frequency_analysis(sine(1_000.0), SAMPLE_RATE)

    with pytest.raises(ValueError, match="sample_rate"):
        compute_spectrum(sine(1_000.0), 44_100, analysis=analysis)


def test_analysis_frame_rejects_mismatched_arrays() -> None:
    """bin周波数と振幅のshape不一致は失敗させる。"""
    with pytest.raises(ValueError, match="shape"):
        FrequencyAnalysisFrame(
            frequencies_hz=np.zeros(3, dtype=np.float64),
            magnitudes=np.zeros(4, dtype=np.float64),
            sample_rate=SAMPLE_RATE,
        )
