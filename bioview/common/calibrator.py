import json
import datetime
from collections import deque
from pathlib import Path

import numpy as np
from scipy.interpolate import PchipInterpolator

from bioview.utils import get_unique_path, TriangleGenerator
from bioview.types import ChannelQualityStatus
from bioview.constants import INIT_DELAY
from bioview.usrp.calibration_transmitter import TRI_FREQ_HZ, NUM_TRIANGLES, INJ_PERIOD_S, TRI_AMP

# ---------------------------------------------------------------------------
# Tunable thresholds (kept out of the UI on purpose)
# ---------------------------------------------------------------------------
MIN_AMPLITUDE = 1e-4       # |h_ref| below this -> POOR (dead cable / antenna)
GOOD_SNR_DB = 20.0
MARGINAL_SNR_DB = 8.0

MIN_SAMPLES_PER_TRI = 20   # save_rate must give at least this many samples per triangle period
MIN_HARMONICS = 5          # baseband bandwidth must pass this many triangle harmonics
MAX_LAG_S = 50e-3          # max expected Tx->Rx pipeline latency; snippet padding on each side.
                           # Generous headroom over typical Python/Qt scheduling jitter around
                           # calibration episode start-up (still tiny vs INJ_PERIOD_S=1.0) - the
                           # correlation-based lag search tolerates arbitrary lag within this
                           # window robustly, so widening it is cheap.
NOISE_WINDOW_S = 5e-3      # pre-burst window used to estimate background noise - deliberately
                           # kept short and decoupled from MAX_LAG_S/pad: pad only needs to be
                           # wide for the *search*, but a wide pre-window risks actually
                           # overlapping the real burst when there's residual lag, which would
                           # make the "noise" estimate partly measure real signal instead.
BASELINE_BURSTS = 5        # first N valid bursts define each channel's healthy baseline
GAIN_DROP_DB = 6.0         # probe gain drop vs baseline -> MARGINAL (antenna loose / moved)
MIN_PROBE_NCC = 0.5        # |NCC(probe, Tx ref)| below this -> probe not seen -> POOR

# Snippet storage for offline CVI / LUT (memory guard)
STORE_ALL_FIRST_N = 60     # keep every burst for the first N
STORE_EVERY_N = 10         # afterwards keep every Nth burst

# CVI grid search (paper Sec. IV-B): translation magnitude/phase steps and the
# search-segment length (a short, well-within-burst window of samples).
CVI_R_STEPS = np.linspace(0.1, 1.0, 10)
CVI_THETA_STEPS = np.linspace(0, 2 * np.pi, 100, endpoint=False)
CVI_SEARCH_LEN = 100

_QUALITY_RANK = {ChannelQualityStatus.GOOD: 0,
                 ChannelQualityStatus.MARGINAL: 1,
                 ChannelQualityStatus.POOR: 2}
_EPS = 1e-30


def _diff_noise_rms(y, mask):
    '''Noise RMS from first differences of contiguous masked samples.
    Slow motion (respiration, pulse) nearly cancels in the difference, so it is
    not mistaken for noise the way a plain variance would be.'''
    if y.size < 2:
        return None, 0
    d = np.diff(y)
    ok = mask[1:] & mask[:-1]
    if not np.any(ok):
        return None, 0
    return float(np.sum(np.abs(d[ok]) ** 2)), int(ok.sum())


class CalibrationAnalyzer:
    '''
    Rx-side calibration capture + live antenna health monitor.

    Fed the already-demodulated per-channel windows from SaveWorker.data_ready.

    It does three things:
      1. Gate-off statistics -> channel reference point h_ref and noise (SNR).
      2. Per-burst probe snippets (complex Rx + commanded Tx triangle, with
         MAX_LAG_S padding on both sides) are kept so CVI/LUT correction
         parameters can be computed from real burst morphology.
      3. Per-burst health metrics (Tx->Rx lag, probe NCC, probe gain, h_ref
         drift) are compared against a baseline so a loose / moved / dead
         antenna can be flagged. Attribution and diagnosis are deferred
         until the whole episode is in (see _resolve_bursts) rather than
         done live burst-by-burst.

    Alignment note: the shadow TriangleGenerator gives the *commanded* gate
    with no latency. Instead of assuming a fixed guard, the latency is
    measured per burst by cross-correlation inside the padded snippet.

    Intended usage: one short-lived instance per calibration episode (e.g.
    "start of recording" / "end of recording"), fed only for the duration of
    that episode's few bursts, then read out via result()/compute_cvi()/
    compute_lut()/probe_view_sample() and discarded.
    '''

    def __init__(self, exp_config, save_iq: bool = True, onset_samples=None):
        # onset_samples: absolute stream index of the first burst onset. None ->
        # assume Tx starts INIT_DELAY after the first chunk (only true if the
        # stream were perfectly fresh - see realigned()).
        self.exp_config = exp_config
        self.save_iq = save_iq
        # Rate of the stream SaveWorker emits on data_ready (stage 1), not
        # the (lower) rate actually written to file
        self.save_rate = float(exp_config.samp_rate) / float(exp_config.analysis_ds())
        self.if_filter_bw = float(exp_config.if_filter_bw)

        self.warnings = []
        self.rate_ok = self._check_rates()

        self.gate_gen = TriangleGenerator(
            fs=self.save_rate, freq=TRI_FREQ_HZ, n_tri=NUM_TRIANGLES, period_s=INJ_PERIOD_S,
            amplitude=TRI_AMP,
        )
        self.burst_len = max(1, int(round(NUM_TRIANGLES / TRI_FREQ_HZ * self.save_rate)))
        self.pad = max(1, int(round(MAX_LAG_S * self.save_rate)))
        # Off-segment exclusion around each burst: latency + filter transient.
        self.guard = self.pad + int(np.ceil(self.save_rate * 5.0 / self.if_filter_bw))

        # CalibrationTransmitWorker schedules its first burst to transmit at
        # usrp.get_time_now() + INIT_DELAY, not immediately when its thread
        # starts - but this shadow clock otherwise starts ticking "period 0"
        # the instant it begins consuming the already-flowing Rx stream, with
        # no knowledge of that scheduled delay. Offset the shadow's starting
        # sample_idx so its first period boundary falls at absolute Rx sample
        # +init_delay_samples (i.e. gate_gen_internal_index = sample_idx +
        # absolute_rx_index, so sample_idx = -init_delay_samples puts the
        # onset there), matching when the real burst actually goes out.
        # Without this, this fixed, known ~50ms delay alone can sit right at
        # (or past) the correlation search's pad boundary. (_ref_wave/_ref_gate
        # are pre-allocated with `guard` zero placeholders before the first
        # _extend_ref() call below concatenates the real generated samples
        # onto them, so absolute Rx sample 0 lands at gate_gen's own sample
        # index `sample_idx` directly - no extra +/- guard term needed here.)
        init_delay_samples = (int(round(INIT_DELAY * self.save_rate))
                              if onset_samples is None else int(onset_samples))
        self.gate_gen.sample_idx = -init_delay_samples

        self._replay = []                   # every payload fed in, for realigned()
        self.window_idx = 0                # absolute index of next incoming sample
        # Commanded reference FIFO, generated `guard` samples ahead of the data
        self._ref_start = -self.guard       # absolute index of _ref_wave[0]
        self._ref_wave = np.zeros(self.guard, dtype=np.float64)
        self._ref_gate = np.zeros(self.guard, dtype=bool)
        self._extend_ref(self.guard)

        self._pending = deque()             # (start_abs, end_abs, burst_no)
        self._burst_no = 0
        self._buf_start = 0
        self._buf = {}                      # label -> complex ndarray, '__tx__' -> commanded wave

        self.stats = {}
        self.bursts = []                    # stored snippets for offline CVI / LUT
        self.burst_metrics = {}             # label -> list of per-burst metric dicts
        self.baseline = {}                  # label -> dict(gain_db, phase_rad)
        self._bursts_by_tx = {}             # tx_idx -> list of (rec, metrics_by_label) for every burst of that role
        self._all_bursts = []               # every (rec, metrics_by_label), raw, pre-attribution
        self._last_metrics = {}             # label -> most recent metrics dict
        self._resolved = False              # whether _resolve_bursts() has run yet
        # Whole episode's |y| per label, kept so realigned() can search the ENTIRE
        # stream for the probe instead of only the +/-MAX_LAG_S window around
        # where the shadow clock assumes it is.
        self._mag = {}                      # label -> list of float32 |y| arrays

        # TDM bookkeeping: which Tx-channel index a label belongs to, and each
        # Tx-channel's own direct-diagonal label (Tx_i's channel_mapping row
        # is also i, by the same Tx_i/Rx_i index convention used elsewhere -
        # see app.py:_generate_usrp_mappings). Used by _finalize_burst to
        # work out which Tx was actually triangle-modulating a given burst
        # period when CalibrationTransmitWorker is round-robin cycling it
        # across Tx channels.
        self._label_tx_idx = {}
        self._diagonal_label = {}           # tx_idx -> its own direct-pair label
        for r_idx, row in enumerate(getattr(exp_config, 'channel_mapping', None) or []):
            for t_idx, label in enumerate(row):
                if not label:
                    continue
                self._label_tx_idx[label] = t_idx
                if r_idx == t_idx:
                    self._diagonal_label[t_idx] = label

    # ------------------------------------------------------------------ setup
    def _check_rates(self):
        ok = True
        need_fs = MIN_SAMPLES_PER_TRI * TRI_FREQ_HZ
        if self.save_rate < need_fs:
            ok = False
            self.warnings.append(
                f'save_rate {self.save_rate:.0f} Hz < {need_fs:.0f} Hz: triangle probe is '
                f'under-sampled, burst morphology cannot be recovered (lower cal_ds).')
        # if_filter_bw assumed to be the full band-pass width around f_IF
        need_bw = 2 * MIN_HARMONICS * TRI_FREQ_HZ
        if self.if_filter_bw < need_bw:
            ok = False
            self.warnings.append(
                f'if_filter_bw {self.if_filter_bw:.0f} Hz < {need_bw:.0f} Hz: triangle harmonics '
                f'are filtered out, probe will look sinusoidal (widen the IF filter).')
        return ok

    def _extend_ref(self, n):
        wave, gate_raw = self.gate_gen.next(n)
        self._ref_wave = np.concatenate([self._ref_wave, np.asarray(wave, dtype=np.float64)])
        self._ref_gate = np.concatenate([self._ref_gate, np.asarray(gate_raw) > 0.5])

    # ----------------------------------------------------------- streaming
    def add_chunk(self, payload):
        ''' payload: {'data': (num_channels, n, 2) array, 'mapping': label -> channel index} '''
        data = payload['data']
        mapping = payload['mapping']
        if data.size == 0:
            return

        self._replay.append(payload)
        n = data.shape[1]
        idx = self.window_idx
        g = self.guard

        # Make the reference cover [idx - g, idx + n + g)
        self._extend_ref(n)
        lo = idx - g - self._ref_start
        ext_gate = self._ref_gate[lo:lo + n + 2 * g]
        wave = self._ref_wave[lo + g:lo + g + n]
        gate = ext_gate[g:g + n]

        # Dilated gate (O(n) running max via cumsum) -> clean off mask
        cs = np.concatenate([[0], np.cumsum(ext_gate.astype(np.int64))])
        near_burst = (cs[2 * g + 1:] - cs[:-(2 * g + 1)]) > 0
        off_mask = ~near_burst

        # Burst onsets in this chunk (commanded timing)
        prev = self._ref_gate[lo + g - 1] if lo + g - 1 >= 0 else False
        rises = np.flatnonzero(np.diff(np.concatenate([[prev], gate]).astype(np.int8)) == 1) + idx
        for r in rises:
            start = r - self.pad
            if start >= 0:          # skip a burst whose leading pad predates the stream
                self._pending.append((start, r + self.burst_len + self.pad, self._burst_no))
            self._burst_no += 1

        # Per-label demod + gate-off statistics
        chunk = {'__tx__': wave}
        for label, ch in mapping.items():
            c0 = data[ch, :, 0]
            c1 = data[ch, :, 1]
            y = c0 + 1j * c1 if self.save_iq else c0 * np.exp(1j * np.radians(c1))
            chunk[label] = y
            self._mag.setdefault(label, []).append(np.abs(y).astype(np.float32))

            st = self._stats_for(label)
            if np.any(off_mask):
                y_off = y[off_mask]
                st['sum_off'] += y_off.sum()
                st['sumsq_off'] += float(np.sum(np.abs(y_off) ** 2))
                st['n_off'] += y_off.size
                dsq, dn = _diff_noise_rms(y, off_mask)
                if dn:
                    st['diffsq_off'] += dsq
                    st['n_diff'] += dn

        # Rolling snippet buffer
        for key, arr in chunk.items():
            old = self._buf.get(key)
            self._buf[key] = arr.copy() if old is None else np.concatenate([old, arr])

        end_abs = idx + n
        while self._pending and self._pending[0][1] <= end_abs:
            self._finalize_burst(*self._pending.popleft())

        keep_from = self._pending[0][0] if self._pending else end_abs - self.pad
        keep_from = max(keep_from, self._buf_start)
        cut = keep_from - self._buf_start
        if cut > 0:
            for key in self._buf:
                self._buf[key] = self._buf[key][cut:]
            self._buf_start = keep_from

        # Drop reference samples no longer needed
        ref_keep = idx + n - g - self._ref_start
        if ref_keep > 0:
            self._ref_wave = self._ref_wave[ref_keep:]
            self._ref_gate = self._ref_gate[ref_keep:]
            self._ref_start += ref_keep

        self.window_idx = end_abs

    def _stats_for(self, label):
        st = self.stats.get(label)
        if st is None:
            st = dict(
                sum_off=0j, sumsq_off=0.0, n_off=0, diffsq_off=0.0, n_diff=0,
            )
            self.stats[label] = st
            self.burst_metrics[label] = []
        return st

    # ------------------------------------------------------- re-anchoring
    def _estimate_onset(self):
        '''Find where the probe really is in the captured stream.

        The shadow clock assumes period 0 starts INIT_DELAY after the first
        chunk, but that chunk is stale (Rx/Save buffering, queue backlog) by an
        unknown, per-episode amount - far more than the +/-MAX_LAG_S search
        window. Slide the triangle template over the whole stream for every
        label, fold the |NCC| peaks modulo INJ_PERIOD (the probe repeats every
        period), and take the strongest bin. Summing NCC**4 across labels and
        periods lets the channels that really carry the probe dominate while
        noise-only channels (median |NCC| ~0.08) contribute almost nothing.
        Returns the burst onset index in [0, period), or None if no clear peak.'''
        L = self.burst_len
        period = int(round(INJ_PERIOD_S * self.save_rate))
        tri, _ = TriangleGenerator(fs=self.save_rate, freq=TRI_FREQ_HZ, n_tri=NUM_TRIANGLES,
                                   period_s=INJ_PERIOD_S, amplitude=TRI_AMP).next(L)
        t = tri.astype(np.float64) - tri.mean()
        t /= max(t.std(), _EPS)

        score = np.zeros(period)
        for parts in self._mag.values():
            mag = np.concatenate(parts).astype(np.float64)
            if mag.size < L + period:
                continue
            num = np.correlate(mag, t, mode='valid') / L
            cs = np.concatenate([[0.0], np.cumsum(mag)])
            cs2 = np.concatenate([[0.0], np.cumsum(mag ** 2)])
            mu = (cs[L:] - cs[:-L]) / L
            var = np.maximum((cs2[L:] - cs2[:-L]) / L - mu ** 2, _EPS)
            ncc = np.abs(num / np.sqrt(var))
            n = (ncc.size // period) * period
            if n:
                score += (ncc[:n].reshape(-1, period) ** 4).sum(axis=0)

        best = int(np.argmax(score))
        if score[best] <= 0 or score[best] < 5.0 * float(np.median(score)):
            return None
        return best

    def realigned(self):
        '''Return a fresh analyzer whose shadow clock is anchored on the burst
        onset actually found in the data, with every buffered chunk replayed
        through it. Everything downstream (fine +/-MAX_LAG_S alignment, gate-off
        statistics, TDM role attribution) is unchanged. Falls back to self if no
        clear probe is found, so a genuinely dead link still reports as POOR.'''
        onset = self._estimate_onset()
        if onset is None:
            self.warnings.append('no clear periodic probe found in the captured stream; '
                                 'kept the default alignment (probe not received?)')
            return self
        new = CalibrationAnalyzer(self.exp_config, save_iq=self.save_iq, onset_samples=onset)
        for payload in self._replay:
            new.add_chunk(payload)
        return new

    # ------------------------------------------------------------ per burst
    def _finalize_burst(self, start, end, burst_no):
        '''Just extract and store the raw per-label metrics for this burst -
        no TDM role attribution or diagnosis yet. With only a handful of
        bursts per episode, deciding "which Tx was active" burst-by-burst
        from noisy per-burst NCC is unreliable (see _resolve_bursts); that
        decision is deferred until the whole episode's evidence is in.'''
        a, b = start - self._buf_start, end - self._buf_start
        tx = self._buf['__tx__'][a:b]
        rec = dict(burst=burst_no, start_sample=int(start), tx_wave=tx.astype(np.float32), rx={})

        metrics = {}
        for label in self._buf:
            if label == '__tx__':
                continue
            y = self._buf[label][a:b]
            if y.size != end - start:
                continue
            rec['rx'][label] = y.astype(np.complex64)
            metrics[label] = self._probe_metrics(label, y, tx)

        self._all_bursts.append((rec, metrics))
        if burst_no < STORE_ALL_FIRST_N or burst_no % STORE_EVERY_N == 0:
            self.bursts.append(rec)

    def _resolve_bursts(self):
        '''Attribute every buffered burst to its true active-Tx role, once,
        using the whole episode's evidence, then run diagnosis. Called
        lazily (idempotent) by result()/compute_cvi()/compute_lut()/
        probe_view_sample() - i.e. only after the episode's
        CalibrationTransmitWorkers have all finished and no more bursts are
        coming in.

        CalibrationTransmitWorker cycles Tx channels in a fixed,
        deterministic round-robin (true active role for period N = N %
        num_tx_channels). The only unknown is a *fixed* integer offset
        between this analyzer's own burst_no=0 and the Tx's true
        period_idx=0 (only num_tx_channels possible values). Resolve that
        offset once by summing diagonal-channel NCC evidence across the
        whole episode for each candidate offset and picking the strongest -
        aggregating over every burst cancels noise, unlike deciding
        burst-by-burst (which breaks down badly when one diagonal channel
        isn't reliably the strongest label, as is the case on this
        hardware): a wrong global offset just consistently swaps which
        physical channel gets which labels for the entire episode, it never
        flickers burst-to-burst the way a per-burst decision can.'''
        if self._resolved:
            return
        self._resolved = True
        if not self._all_bursts:
            return

        n_tx = len(self._diagonal_label)
        offset = None
        if n_tx > 1:
            best_offset, best_score = 0, None
            for candidate in range(n_tx):
                score = 0.0
                for rec, metrics in self._all_bursts:
                    active_tx = (rec['burst'] + candidate) % n_tx
                    label = self._diagonal_label.get(active_tx)
                    m = metrics.get(label) if label else None
                    if m is not None:
                        score += abs(m['ncc'])
                if best_score is None or score > best_score:
                    best_offset, best_score = candidate, score
            offset = best_offset

        for rec, metrics in self._all_bursts:
            burst_no = rec['burst']
            active_tx = (burst_no + offset) % n_tx if offset is not None else None
            if active_tx is not None:
                self._bursts_by_tx.setdefault(active_tx, []).append((rec, metrics))

            for label, m in metrics.items():
                if active_tx is not None and self._label_tx_idx.get(label) != active_tx:
                    continue   # this label's Tx wasn't modulating this burst period - not a real measurement
                m['burst'] = burst_no
                m['start_sample'] = rec['start_sample']
                self.burst_metrics.setdefault(label, []).append(m)

        for label, hist in self.burst_metrics.items():
            if not hist:
                continue
            m = hist[-1]
            m['quality'], m['reason'], m['mean_gain_db'], m['mean_snr_db'], m['mean_ncc'] = self._diagnose(label)
            self._last_metrics[label] = m

    def _probe_metrics(self, label, y, tx):
        L, P = self.burst_len, self.pad
        tri = tx[P:P + L]
        # Deliberately short and decoupled from P (=pad, now wide to tolerate
        # search-timing slop) - a wide pre-window risks actually overlapping
        # the real burst when there's residual lag, which would make this
        # "off" reference partly measure real triangle-modulation energy
        # instead of background noise.
        noise_win = min(P, max(1, int(round(NOISE_WINDOW_S * self.save_rate))))
        pre = y[:noise_win]
        h = complex(pre.mean())
        noise_sq, noise_n = _diff_noise_rms(pre, np.ones(pre.size, dtype=bool))
        if noise_n:
            noise_rms = float(np.sqrt(noise_sq / noise_n / 2.0))
        else:
            # Pre-burst pad too short to estimate noise on its own (e.g. very
            # high save_rate collapsing `pad` to ~1 sample) - fall back to the
            # channel's aggregate gate-off estimate rather than 0.0, which
            # would blow up probe_snr_db via the _EPS floor below and falsely
            # read as GOOD.
            st = self.stats.get(label)
            noise_rms = (float(np.sqrt(st['diffsq_off'] / st['n_diff'] / 2.0))
                         if st and st['n_diff'] else 0.0)

        mag = np.abs(y).astype(np.float64)
        t = tri - tri.mean()
        t_std = t.std()
        m = dict(h_amp=abs(h), h_phase_deg=float(np.degrees(np.angle(h))),
                 noise_rms=float(noise_rms))

        if t_std < _EPS or mag.size < L:
            m.update(lag_samples=0, ncc=0.0, probe_gain_db=-np.inf,
                     linearity=0.0, probe_snr_db=0.0)
        else:
            t /= t_std
            num = np.correlate(mag, t, mode='valid') / L
            cs = np.concatenate([[0.0], np.cumsum(mag)])
            cs2 = np.concatenate([[0.0], np.cumsum(mag ** 2)])
            mu = (cs[L:] - cs[:-L]) / L
            var = np.maximum((cs2[L:] - cs2[:-L]) / L - mu ** 2, _EPS)
            ncc = num / np.sqrt(var)
            k = int(np.argmax(np.abs(ncc)))

            seg = mag[k:k + L]
            slope = float(np.polyfit(tri, seg, 1)[0])
            probe_snr_db = (float(20 * np.log10(max(abs(slope) * TRI_AMP, _EPS) / noise_rms))
                            if noise_rms > 0 else 0.0)
            m.update(
                lag_samples=k - P,
                ncc=float(ncc[k]),
                probe_gain_db=float(20 * np.log10(max(abs(slope), _EPS))),
                # ~1 for a linear channel (|y| ~ a(1 + tri)); <1 means compression
                linearity=float(abs(slope) / max(abs(h), _EPS)),
                probe_snr_db=probe_snr_db,
            )

        m['artifact_hint'] = self._artifact_hint(m)
        return m

    @staticmethod
    def _artifact_hint(m):
        '''Raw-magnitude hint only. Biphasic twist depends on the recentered
        trajectory, so the definitive check is the offline CVI analysis.'''
        c = m['ncc']
        if abs(c) < MIN_PROBE_NCC:
            return 'probe_not_seen'
        if c < 0:
            return 'phase_reversal_suspect'
        if m['linearity'] < 0.8:
            return 'compression_suspect'
        if c < 0.9:
            return 'morphology_distorted'
        return 'none'

    def _diagnose(self, label):
        '''Whole-run-average, 3-axis diagnosis: gain / SNR / shape-match
        (NCC) are evaluated separately, since a single blended SNR number
        conflates channel gain with noise and flickers burst to burst.
        Averaging over every burst captured so far (not a trailing window)
        is what keeps the reported quality stable.'''
        hist = self.burst_metrics.get(label, [])
        if not hist:
            reason = 'no complete burst captured'
            if not self.rate_ok:
                reason = f'[CONFIG] {"; ".join(self.warnings)} -- {reason}'
            return (ChannelQualityStatus.POOR, reason,
                    float('nan'), float('nan'), float('nan'))

        mean_h_amp = float(np.mean([x['h_amp'] for x in hist]))
        finite_gains = [x['probe_gain_db'] for x in hist if np.isfinite(x['probe_gain_db'])]
        mean_gain_db = float(np.mean(finite_gains)) if finite_gains else float('nan')
        mean_snr_db = float(np.mean([x['probe_snr_db'] for x in hist]))
        mean_ncc = float(np.mean([abs(x['ncc']) for x in hist]))

        # Baseline gain/phase from the first BASELINE_BURSTS valid bursts.
        valid_hist = [x for x in hist if abs(x['ncc']) >= MIN_PROBE_NCC]
        base = self.baseline.get(label)
        if base is None and len(valid_hist) >= BASELINE_BURSTS:
            recent = valid_hist[:BASELINE_BURSTS]
            base = dict(gain_db=float(np.median([x['probe_gain_db'] for x in recent])),
                        phase_deg=float(np.median([x['h_phase_deg'] for x in recent])))
            self.baseline[label] = base

        if mean_h_amp < MIN_AMPLITUDE:
            return (ChannelQualityStatus.POOR, 'no carrier (cable / antenna disconnected?)',
                    mean_gain_db, mean_snr_db, mean_ncc)

        verdicts, reasons = [], []

        if base is not None and np.isfinite(mean_gain_db):
            gain_drop = base['gain_db'] - mean_gain_db
            if gain_drop > GAIN_DROP_DB:
                verdicts.append(ChannelQualityStatus.MARGINAL)
                reasons.append(f'gain dropped {gain_drop:.1f}dB vs baseline '
                              f'(antenna loose / moved / cable / Rx front-end?)')
            else:
                verdicts.append(ChannelQualityStatus.GOOD)

            # Whole-run mean above is diluted by earlier good bursts and can
            # miss a fault that develops partway through the run - also check
            # just the most recent BASELINE_BURSTS bursts against baseline.
            recent_gains = [x['probe_gain_db'] for x in hist[-BASELINE_BURSTS:]
                            if np.isfinite(x['probe_gain_db'])]
            if recent_gains:
                recent_gain_drop = base['gain_db'] - float(np.mean(recent_gains))
                if recent_gain_drop > GAIN_DROP_DB:
                    verdicts.append(ChannelQualityStatus.POOR)
                    reasons.append(f'gain dropped {recent_gain_drop:.1f}dB vs baseline in the '
                                  f'last {len(recent_gains)} bursts (antenna loose / moved now?)')
        else:
            verdicts.append(ChannelQualityStatus.GOOD)   # not enough data yet to judge gain drift

        if mean_snr_db < MARGINAL_SNR_DB:
            verdicts.append(ChannelQualityStatus.POOR)
            reasons.append(f'low SNR {mean_snr_db:.1f}dB (interference / gain setting / grounding?)')
        elif mean_snr_db < GOOD_SNR_DB:
            verdicts.append(ChannelQualityStatus.MARGINAL)
            reasons.append(f'marginal SNR {mean_snr_db:.1f}dB')
        else:
            verdicts.append(ChannelQualityStatus.GOOD)

        if mean_ncc < MIN_PROBE_NCC:
            verdicts.append(ChannelQualityStatus.POOR)
            reasons.append(f'probe shape mismatch ncc={mean_ncc:.2f} (probe not detected / distorted)')
        elif mean_ncc < 0.9:
            verdicts.append(ChannelQualityStatus.MARGINAL)
            reasons.append(f'morphology distorted ncc={mean_ncc:.2f}')
        else:
            verdicts.append(ChannelQualityStatus.GOOD)

        quality = max(verdicts, key=lambda q: _QUALITY_RANK[q])
        reason = '; '.join(reasons) if reasons else 'ok'
        if not self.rate_ok:
            reason = f'[CONFIG] {"; ".join(self.warnings)} -- {reason}'
        return quality, reason, mean_gain_db, mean_snr_db, mean_ncc

    # --------------------------------------------------------------- output
    def result(self):
        self._resolve_bursts()
        channels = {}
        for label, st in self.stats.items():
            bm = self.burst_metrics.get(label, [])
            base = dict(n_bursts=len(bm), baseline=self.baseline.get(label))

            if st['n_off'] == 0:
                channels[label] = dict(base, amplitude=0.0, phase_rad=0.0,
                                       complex_factor_re=0.0, complex_factor_im=0.0,
                                       snr_db=0.0, quality=ChannelQualityStatus.POOR,
                                       error='no gate-off samples captured')
                continue

            h_ref = st['sum_off'] / st['n_off']
            amplitude = float(abs(h_ref))
            phase_rad = float(np.angle(h_ref))
            noise_rms = (np.sqrt(st['diffsq_off'] / st['n_diff'] / 2.0)
                         if st['n_diff'] else 0.0)
            snr_db = 200.0 if noise_rms < _EPS else float(20 * np.log10(max(amplitude, _EPS) / noise_rms))

            quality, reason, mean_gain_db, mean_snr_db, mean_ncc = self._diagnose(label)
            if amplitude < MIN_AMPLITUDE:
                quality, reason = ChannelQualityStatus.POOR, 'no carrier (cable / antenna disconnected?)'
                if not self.rate_ok:
                    reason = f'[CONFIG] {"; ".join(self.warnings)} -- {reason}'

            last = bm[-1] if bm else {}
            channels[label] = dict(
                base,
                amplitude=amplitude,
                phase_rad=phase_rad,
                phase_deg=float(np.degrees(phase_rad)),
                complex_factor_re=float(h_ref.real),
                complex_factor_im=float(h_ref.imag),
                snr_db=snr_db,
                n_samples_off=int(st['n_off']),
                quality=quality,
                reason=reason,
                mean_gain_db=mean_gain_db,
                mean_snr_db=mean_snr_db,
                mean_ncc=mean_ncc,
                lag_samples=last.get('lag_samples'),
                ncc=last.get('ncc'),
                probe_gain_db=last.get('probe_gain_db'),
                linearity=last.get('linearity'),
                probe_snr_db=last.get('probe_snr_db'),
                artifact_hint=last.get('artifact_hint'),
            )
        return channels

    def burst_arrays(self):
        '''Stack stored snippets into arrays for np.savez.'''
        if not self.bursts:
            return {}
        labels = sorted({k for b in self.bursts for k in b['rx']})
        out = dict(
            burst_no=np.array([b['burst'] for b in self.bursts]),
            start_sample=np.array([b['start_sample'] for b in self.bursts]),
            tx_wave=np.stack([b['tx_wave'] for b in self.bursts]),
        )
        for lb in labels:
            out[f'rx__{lb}'] = np.stack([
                b['rx'].get(lb, np.full(b['tx_wave'].shape, np.nan, dtype=np.complex64))
                for b in self.bursts])
        return out

    # ------------------------------------------------------------- CVI / LUT
    def _bursts_for_label(self, label):
        ''' (rec, metrics) pairs where `label`'s own Tx was confirmed active
        for that burst, falling back to every burst when there's no TDM role
        information (e.g. a single-Tx-channel device). '''
        self._resolve_bursts()
        tx_idx = self._label_tx_idx.get(label)
        bursts = self._bursts_by_tx.get(tx_idx) if tx_idx is not None else None
        return bursts if bursts else self._all_bursts

    def compute_cvi(self, label):
        '''Paper Sec. IV-B: grid search over a complex translation r*e^(j*theta)
        that maximizes NCC between the corrected Rx magnitude and the Tx
        reference, evaluated on a short (<=100 sample) window near the start
        of each stored burst for this label. Averaged (circular mean for
        theta) across every stored burst for robustness against a single
        noisy burst - this is the value written to the saved calibration
        JSON, independent of whichever burst the probe-view dialog happens
        to display.'''
        L, P = self.burst_len, self.pad
        seg_len = min(CVI_SEARCH_LEN, L)

        rs, thetas, nccs = [], [], []
        for rec, _metrics in self._bursts_for_label(label):
            rx = rec['rx'].get(label)
            if rx is None:
                continue
            z = rx[P:P + seg_len].astype(np.complex128)
            tri = rec['tx_wave'][P:P + seg_len].astype(np.float64)
            if tri.std() < _EPS:
                continue

            best = None
            for r in CVI_R_STEPS:
                shifts = r * np.exp(1j * CVI_THETA_STEPS)
                for theta, shift in zip(CVI_THETA_STEPS, shifts):
                    mag = np.abs(z + shift)
                    if mag.std() < _EPS:
                        continue
                    ncc = float(np.corrcoef(mag, tri)[0, 1])
                    if best is None or abs(ncc) > abs(best[2]):
                        best = (float(r), float(theta), ncc)
            if best is not None:
                rs.append(best[0])
                thetas.append(best[1])
                nccs.append(best[2])

        if not rs:
            return None
        theta_mean = float(np.angle(np.mean(np.exp(1j * np.asarray(thetas))))) % (2 * np.pi)
        return dict(r=float(np.mean(rs)), theta=theta_mean, ncc=float(np.mean(nccs)), n_bursts=len(rs))

    def compute_lut(self, label):
        '''Paper Eq. 10: PCHIP mapping from distorted Rx magnitude to the
        ideal Tx reference, fit separately on the rising and falling ramps of
        the triangle (known exactly from the commanded tx_wave, so no
        peak/valley detection on noisy Rx data is needed). Pools samples
        across every stored burst for this label. Returns sorted,
        deduplicated (rx, tx) knot points rather than a fitted interpolator
        object, so this is directly JSON-serializable; PchipInterpolator can
        be reconstructed from these knots by whoever consumes the file.'''
        L, P = self.burst_len, self.pad
        rise_rx, rise_tx, fall_rx, fall_tx = [], [], [], []

        for rec, _metrics in self._bursts_for_label(label):
            rx = rec['rx'].get(label)
            if rx is None:
                continue
            y = np.abs(rx[P:P + L]).astype(np.float64)
            tri = rec['tx_wave'][P:P + L].astype(np.float64)
            if len(tri) < 2:
                continue
            d = np.diff(tri)
            rising = np.concatenate([[d[0] >= 0], d >= 0])
            rise_rx.extend(y[rising].tolist())
            rise_tx.extend(tri[rising].tolist())
            fall_rx.extend(y[~rising].tolist())
            fall_tx.extend(tri[~rising].tolist())

        def fit_ramp(rx_vals, tx_vals):
            if len(rx_vals) < 2:
                return None
            rx_arr, tx_arr = np.asarray(rx_vals), np.asarray(tx_vals)
            order = np.argsort(rx_arr)
            rx_sorted, tx_sorted = rx_arr[order], tx_arr[order]
            rx_unique, first_idx = np.unique(rx_sorted, return_index=True)
            if len(rx_unique) < 2:
                return None
            tx_unique = tx_sorted[first_idx]
            # Sanity-check the fit is at least constructible before saving it.
            PchipInterpolator(rx_unique, tx_unique)
            return dict(rx=rx_unique.tolist(), tx=tx_unique.tolist())

        rise = fit_ramp(rise_rx, rise_tx)
        fall = fit_ramp(fall_rx, fall_tx)
        if rise is None and fall is None:
            return None
        return dict(rise=rise, fall=fall)

    # --------------------------------------------------------------- display
    def _window_view(self, rec, m, margin):
        L, P = self.burst_len, self.pad
        rx = rec['rx'].get
        tx = rec['tx_wave'].astype(np.float64)
        lo_tx = max(0, P - margin)
        hi_tx = min(len(tx), P + L + margin)
        tx_win = tx[lo_tx:hi_tx]
        t_ms = (np.arange(lo_tx, hi_tx) - P) / self.save_rate * 1000.0
        lag, h_amp = m['lag_samples'], m['h_amp']
        return lo_tx, hi_tx, lag, h_amp, tx_win, t_ms

    def probe_view_sample(self, margin_s: float = 0.25e-3):
        '''Lag-aligned, normalized view of the *best* burst per label (highest
        |NCC| whose window fits in the stored snippet) for a quick visual
        sanity check - {label: dict(t_ms, rx_norm, tx_norm, ncc) or None}.
        A label with no usable burst maps to None rather than being omitted, so
        the caller can show the failure instead of silently dropping the plot.
        margin_s=0.25ms each side of the 2.5ms burst gives a ~3ms window.

        With TDM injection (see CalibrationTransmitWorker), only one Tx
        channel is triangle-modulated in any given burst period, so each
        label draws from its own Tx role's stored bursts (_bursts_for_label)
        rather than a single shared "last burst" - a label whose Tx wasn't
        active in a given burst would have no real triangle in its Rx data,
        and pairing that with the shared reference triangle would look like a
        failed/noisy channel rather than simply an idle one.'''
        margin = max(1, int(round(margin_s * self.save_rate)))

        out = {}
        for label in self._label_tx_idx:
            candidates = self._bursts_for_label(label)
            candidates = [(rec, metrics) for rec, metrics in candidates if metrics.get(label) is not None]
            candidates.sort(key=lambda c: abs(c[1][label]['ncc']), reverse=True)

            out[label] = None
            for rec, metrics in candidates:
                m = metrics[label]
                rx = rec['rx'].get(label)
                if rx is None:
                    continue
                lo_tx, hi_tx, lag, h_amp, tx_win, t_ms = self._window_view(rec, m, margin)
                lo_rx, hi_rx = lo_tx + lag, hi_tx + lag
                if lo_rx < 0 or hi_rx > len(rx) or h_amp < _EPS:
                    continue    # lag pushed the window out of the padded snippet - try the next-best burst
                mag = np.abs(rx[lo_rx:hi_rx]).astype(np.float64)
                out[label] = dict(t_ms=t_ms,
                                  rx_norm=(mag - h_amp) / (h_amp * TRI_AMP),
                                  tx_norm=tx_win / TRI_AMP,
                                  ncc=float(m['ncc']))
                break
        return out


def _jsonable(x):
    if isinstance(x, dict):
        return {k: _jsonable(v) for k, v in x.items()}
    if isinstance(x, (list, tuple)):
        return [_jsonable(v) for v in x]
    if isinstance(x, ChannelQualityStatus):
        return x.value[0]
    if isinstance(x, np.generic):
        x = x.item()
    if isinstance(x, float) and not np.isfinite(x):
        return None
    return x


def write_calibration_result(exp_config, usrp_config, phase_channels):
    '''
    Save the consolidated start/end calibration results for one recording to
    <file_name>_calibration.json, alongside a snapshot of the recording's own
    configuration ("presettings") for later interpretation.

    phase_channels: {'start': {label: {...result..., 'cvi':..., 'lut':...}},
                      'end':   {label: {...}}}
    '''
    save_dir = Path(exp_config.save_dir)
    save_dir.mkdir(parents=True, exist_ok=True)
    out_path = Path(get_unique_path(str(save_dir), f'{exp_config.file_name}_calibration.json'))

    devices = []
    for cfg in (usrp_config or []):
        devices.append(dict(
            device_name=cfg.device_name,
            carrier_freq=cfg.get_param_value('carrier_freq'),
            if_freq=cfg.get_param_value('if_freq'),
            rx_gain=cfg.get_param_value('rx_gain'),
            tx_gain=cfg.get_param_value('tx_gain'),
            tx_amplitude=cfg.get_param_value('tx_amplitude'),
            samp_rate=cfg.get_param_value('samp_rate'),
            rx_channels=cfg.get_param_value('rx_channels'),
            tx_channels=cfg.get_param_value('tx_channels'),
        ))

    presettings = dict(
        file_name=exp_config.file_name,
        save_dir=exp_config.save_dir,
        samp_rate=exp_config.samp_rate,
        save_ds=exp_config.save_ds,
        cal_ds=exp_config.analysis_ds(),
        disp_ds=exp_config.disp_ds,
        if_filter_bw=exp_config.if_filter_bw,
        save_phase=exp_config.save_phase,
        channel_mapping=exp_config.channel_mapping,
        devices=devices,
    )

    probe = dict(tri_freq_hz=TRI_FREQ_HZ, num_triangles=NUM_TRIANGLES,
                 inj_period_s=INJ_PERIOD_S, tri_amp=TRI_AMP)

    labels = sorted({label for ch in phase_channels.values() for label in ch})
    channels = {label: {phase: ch.get(label) for phase, ch in phase_channels.items()}
                for label in labels}

    doc = dict(
        schema_version=3,
        generator='bioview.common.calibrator',
        timestamp_iso=datetime.datetime.now().isoformat(),
        presettings=presettings,
        probe=probe,
        channels=channels,
    )

    with open(out_path, 'w') as f:
        json.dump(_jsonable(doc), f, indent=2)

    return str(out_path)
