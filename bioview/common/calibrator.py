import json
import datetime
import numpy as np

from pathlib import Path

from bioview.utils import get_unique_path, TriangleGenerator
from bioview.types import ChannelQualityStatus
from bioview.usrp.calibration_transmitter import TRI_FREQ_HZ, NUM_TRIANGLES, INJ_PERIOD_S, TRI_AMP

# Internal, tunable-by-editing-code thresholds (kept out of the UI on purpose)
MIN_AMPLITUDE = 1e-4     # below this, a channel is force-flagged POOR (likely dead/disconnected)
GOOD_SNR_DB = 20.0
MARGINAL_SNR_DB = 8.0

# AM-to-AM curve: number of commanded-amplitude bins spanning [1-TRI_AMP, 1+TRI_AMP]
# (the actual (1 + triangle) envelope CalibrationTransmitWorker applies to the carrier)
NUM_AM_BINS = 16


class CalibrationAnalyzer:
    '''
    Rx-side calibration analysis. Ports the standalone B210 calibration
    script's single-operating-point CalibrationCollector onto bioview's own
    channel-mapping conventions, but instead of demodulating raw IQ itself,
    it's fed the already-demodulated per-channel-pair windows that
    SaveWorker._process() computes (via SaveWorker.data_ready) — the same
    bandpass-filter + phase-continuous-downconversion normal recording
    already uses, so calibration doesn't duplicate that DSP.

    The triangle burst's inter-burst ("gate-off") segments are a known,
    constant excitation — the transmitter never goes silent — so the complex
    mean of the demodulated windows there *is* the channel's I/Q reference
    point (the transfer coefficient for that Tx->Rx pair).

    The burst ("gate-on") segments aren't just a single on/off point either —
    within a burst the triangle continuously ramps the commanded envelope
    across its full range (0.5x-1.5x of the base carrier amplitude, for the
    default TRI_AMP=0.5), sweeping through it 2*NUM_TRIANGLES times per
    period. Binning every sample (on AND off) by its own instantaneous
    commanded amplitude therefore builds a full AM-to-AM response curve
    (measured amplitude vs. commanded amplitude) per channel from a single
    fixed-amplitude run, with no need for a separate amplitude sweep.
    '''
    def __init__(self, exp_config, save_iq: bool = True):
        self.exp_config = exp_config
        self.save_iq = save_iq
        self.save_rate = float(exp_config.samp_rate) / float(exp_config.save_ds)
        self.if_filter_bw = float(exp_config.if_filter_bw)

        self.stats = {}  # populated lazily as labels are seen in incoming payloads

        # Shadow gate/wave generator running at the *window* rate SaveWorker outputs at, with
        # the same timing AND amplitude params as CalibrationTransmitWorker, so the values it
        # reconstructs match the real commanded envelope exactly. Its counter starts at 0 here,
        # at the same moment CalibrationTransmitWorker's does, so only the roughly-constant
        # Tx->Rx pipeline latency needs absorbing (via a guard band), not true timestamp sync.
        self.gate_gen = TriangleGenerator(
            fs=self.save_rate, freq=TRI_FREQ_HZ, n_tri=NUM_TRIANGLES, period_s=INJ_PERIOD_S,
            amplitude=TRI_AMP,
        )
        self.guard = max(1, int(round(self.save_rate * max(5.0 / self.if_filter_bw, 0.02 * INJ_PERIOD_S))))
        self.window_idx = 0

        # Bin edges for the commanded envelope (1 + triangle), i.e. [1-TRI_AMP, 1+TRI_AMP]
        self.am_bin_edges = np.linspace(1.0 - TRI_AMP, 1.0 + TRI_AMP, NUM_AM_BINS + 1)

    def _masks(self, n):
        wave, gate_raw = self.gate_gen.next(n)
        gate = gate_raw > 0.5
        on, off = gate.copy(), ~gate
        if self.guard > 0:
            edges = np.flatnonzero(np.diff(gate.astype(np.int8)) != 0) + 1
            for e in edges:
                lo, hi = max(0, e - self.guard), min(n, e + self.guard)
                on[lo:hi] = False
                off[lo:hi] = False
        return wave, on, off

    def _stats_for(self, label):
        st = self.stats.get(label)
        if st is None:
            st = dict(
                sum_off=0j, sumsq_off=0.0, n_off=0, sum_on=0j, n_on=0,
                am_sum=np.zeros(NUM_AM_BINS), am_count=np.zeros(NUM_AM_BINS, dtype=np.int64),
            )
            self.stats[label] = st
        return st

    def add_chunk(self, payload):
        ''' payload: {'data': (num_channels, n, 2) array, 'mapping': label -> channel index} '''
        data = payload['data']
        mapping = payload['mapping']
        if data.size == 0:
            return

        n = data.shape[1]
        wave, on_mask, off_mask = self._masks(n)

        # Instantaneous commanded envelope (1 + triangle) at every sample - already 0 (i.e.
        # commanded == 1.0) outside the burst, since TriangleGenerator gates `wave` itself
        commanded = 1.0 + wave.astype(np.float64)
        bin_idx = np.clip(np.digitize(commanded, self.am_bin_edges) - 1, 0, NUM_AM_BINS - 1)
        valid = on_mask | off_mask

        for label, channel_idx in mapping.items():
            comp0 = data[channel_idx, :, 0]
            comp1 = data[channel_idx, :, 1]
            if self.save_iq:
                y = comp0 + 1j * comp1
            else:
                y = comp0 * np.exp(1j * np.radians(comp1))

            st = self._stats_for(label)
            if np.any(off_mask):
                y_off = y[off_mask]
                st['sum_off'] += y_off.sum()
                st['sumsq_off'] += float(np.sum(np.abs(y_off) ** 2))
                st['n_off'] += y_off.size
            if np.any(on_mask):
                y_on = y[on_mask]
                st['sum_on'] += y_on.sum()
                st['n_on'] += y_on.size

            if np.any(valid):
                idxs = bin_idx[valid]
                mags = np.abs(y[valid])
                st['am_sum'] += np.bincount(idxs, weights=mags, minlength=NUM_AM_BINS)
                st['am_count'] += np.bincount(idxs, minlength=NUM_AM_BINS).astype(np.int64)

        self.window_idx += n

    def _am_to_am(self, st):
        centers = (self.am_bin_edges[:-1] + self.am_bin_edges[1:]) / 2
        counts = st['am_count']
        have_data = counts > 0
        response = np.divide(st['am_sum'], counts, out=np.zeros_like(st['am_sum']), where=have_data)
        return dict(
            commanded_amplitude=centers[have_data].tolist(),
            response_amplitude=response[have_data].tolist(),
            n_samples=counts[have_data].tolist(),
        )

    def result(self):
        channels = {}
        for label, st in self.stats.items():
            n_off = st['n_off']
            if n_off == 0:
                channels[label] = dict(
                    amplitude=0.0, phase_rad=0.0, complex_factor_re=0.0, complex_factor_im=0.0,
                    snr_db=0.0, quality=ChannelQualityStatus.POOR,
                    error='no gate-off samples captured',
                    am_to_am=self._am_to_am(st),
                )
                continue

            h_ref = st['sum_off'] / n_off
            amplitude = float(abs(h_ref))
            phase_rad = float(np.angle(h_ref))
            variance_off = max(st['sumsq_off'] / n_off - amplitude ** 2, 0.0)
            noise_rms = float(np.sqrt(variance_off))
            snr_db = 200.0 if noise_rms < 1e-30 else 20 * np.log10(max(amplitude, 1e-300) / noise_rms)

            if amplitude < MIN_AMPLITUDE:
                quality = ChannelQualityStatus.POOR
            elif snr_db >= GOOD_SNR_DB:
                quality = ChannelQualityStatus.GOOD
            elif snr_db >= MARGINAL_SNR_DB:
                quality = ChannelQualityStatus.MARGINAL
            else:
                quality = ChannelQualityStatus.POOR

            channels[label] = dict(
                amplitude=amplitude,
                phase_rad=phase_rad,
                phase_deg=float(np.degrees(phase_rad)),
                complex_factor_re=float(h_ref.real),
                complex_factor_im=float(h_ref.imag),
                snr_db=float(snr_db),
                n_samples_off=int(n_off),
                n_samples_on=int(st['n_on']),
                quality=quality,
                am_to_am=self._am_to_am(st),
            )

        return channels


def write_calibration_result(exp_config, usrp_config, channels):
    '''
    Save calibration channel results (including each channel's I/Q reference
    point) to a plain JSON file next to the experiment's regular data files.
    '''
    save_dir = Path(exp_config.save_dir)
    save_dir.mkdir(parents=True, exist_ok=True)
    out_path = get_unique_path(str(save_dir), f'{exp_config.file_name}_cal.json')

    cfg0 = usrp_config[0] if usrp_config else None
    provenance = {}
    if cfg0 is not None:
        provenance = dict(
            device_name=cfg0.device_name,
            carrier_freq=cfg0.get_param_value('carrier_freq'),
            if_freq=cfg0.get_param_value('if_freq'),
            rx_gain=cfg0.get_param_value('rx_gain'),
            tx_gain=cfg0.get_param_value('tx_gain'),
            samp_rate=cfg0.get_param_value('samp_rate'),
            if_filter_bw=exp_config.if_filter_bw,
        )

    doc = dict(
        schema_version=1,
        generator='bioview.common.calibrator',
        timestamp_iso=datetime.datetime.now().isoformat(),
        provenance=provenance,
        channels={
            label: {**ch, 'quality': ch['quality'].value[0]}
            for label, ch in channels.items()
        },
    )

    with open(out_path, 'w') as f:
        json.dump(doc, f, indent=2)

    return str(out_path)
