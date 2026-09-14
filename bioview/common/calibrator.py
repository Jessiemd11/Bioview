import json
import queue
import datetime
import numpy as np

from pathlib import Path
from PyQt6.QtCore import QThread, pyqtSignal

from bioview.utils import get_filter, apply_filter, get_unique_path, TriangleGenerator
from bioview.types import ChannelQualityStatus
from bioview.usrp.calibration_transmitter import TRI_FREQ_HZ, NUM_TRIANGLES, INJ_PERIOD_S

# Internal, tunable-by-editing-code thresholds (kept out of the UI on purpose)
MIN_AMPLITUDE = 1e-4     # below this, a channel is force-flagged POOR (likely dead/disconnected)
GOOD_SNR_DB = 20.0
MARGINAL_SNR_DB = 8.0
GOOD_REL_STD = 0.05
MARGINAL_REL_STD = 0.15
GOOD_PHASE_STD_RAD = 0.05
MARGINAL_PHASE_STD_RAD = 0.2


class CalibrationAnalyzer:
    '''
    Rx-side calibration analysis. Ports the standalone B210 calibration
    script's single-operating-point CalibrationCollector onto bioview's own
    channel-mapping conventions and demodulation approach (bandpass filter +
    phase-continuous downconversion, same as SaveWorker._process_chunk),
    working directly off raw IQ buffers pulled from a dedicated Rx queue
    (not through SaveWorker).

    The triangle burst's inter-burst ("gate-off") segments are a known,
    constant excitation — the transmitter never goes silent — so the complex
    mean of demodulated baseband there *is* the channel's I/Q reference
    point (the transfer coefficient for that Tx->Rx pair).
    '''
    def __init__(self, exp_config):
        self.exp_config = exp_config
        self.samp_rate = float(exp_config.samp_rate)
        self.channel_ifs = exp_config.channel_ifs
        self.channel_mapping = exp_config.channel_mapping
        self.if_filter_bw = float(exp_config.if_filter_bw)

        # One bandpass filter per Tx column index, exactly as SaveWorker._load_filter does
        self.if_filts = []
        for if_freq in self.channel_ifs:
            low = if_freq - self.if_filter_bw / 2
            high = if_freq + self.if_filter_bw / 2
            self.if_filts.append(get_filter(bounds=[low, high], samp_rate=self.samp_rate,
                                            btype='band', order=2))

        # Per-channel-pair label state (filter zi + downconversion phase must stay
        # independent per label even when two labels share the same IF filter coefficients,
        # since they filter different Rx rows)
        self.labels = []
        for row in self.channel_mapping:
            for label in row:
                if label:
                    self.labels.append(label)

        self.filter_states = {label: None for label in self.labels}
        self.phase_accumulator = {label: 0.0 for label in self.labels}
        self.stats = {label: dict(
            sum_off=0j, sumsq_off=0.0, n_off=0,
            sum_on=0j, n_on=0,
        ) for label in self.labels}

        # Shadow gate generator: same timing params as CalibrationTransmitWorker, counter
        # starting at 0 here too (both start together when calibration begins), so only the
        # roughly-constant Tx->Rx pipeline latency needs absorbing, via a guard band
        self.gate_gen = TriangleGenerator(
            fs=self.samp_rate, freq=TRI_FREQ_HZ, n_tri=NUM_TRIANGLES, period_s=INJ_PERIOD_S,
        )
        self.guard = max(1, int(round(self.samp_rate * max(5.0 / self.if_filter_bw, 0.02 * INJ_PERIOD_S))))
        self.sample_idx = 0

    def _masks(self, n):
        gate = self.gate_gen.next(n)[1] > 0.5
        on, off = gate.copy(), ~gate
        if self.guard > 0:
            edges = np.flatnonzero(np.diff(gate.astype(np.int8)) != 0) + 1
            for e in edges:
                lo, hi = max(0, e - self.guard), min(n, e + self.guard)
                on[lo:hi] = False
                off[lo:hi] = False
        return on, off

    def _demod(self, data, label, t_idx, if_freq):
        filt = self.if_filts[t_idx]
        zi = self.filter_states[label]
        filt_data, zf = apply_filter(data, filt, zi=zi)
        self.filter_states[label] = zf

        phase0 = self.phase_accumulator[label]
        phase_increment = 2 * np.pi * if_freq / self.samp_rate
        phases = phase0 + np.arange(len(filt_data)) * phase_increment
        baseband = (filt_data * np.exp(-1j * phases)).astype(np.complex64)
        if len(phases) > 0:
            self.phase_accumulator[label] = float(phases[-1] + phase_increment)

        return baseband

    def add_chunk(self, buffer):
        ''' buffer: (num_rx_channels, n) complex64, raw IQ straight off the streamer '''
        n = buffer.shape[1]
        on_mask, off_mask = self._masks(n)

        for r_idx, row in enumerate(self.channel_mapping):
            x = buffer[r_idx, :]
            for t_idx, label in enumerate(row):
                if not label:
                    continue
                y = self._demod(x, label, t_idx, self.channel_ifs[t_idx])
                st = self.stats[label]
                if np.any(off_mask):
                    y_off = y[off_mask]
                    st['sum_off'] += y_off.sum()
                    st['sumsq_off'] += float(np.sum(np.abs(y_off) ** 2))
                    st['n_off'] += y_off.size
                if np.any(on_mask):
                    y_on = y[on_mask]
                    st['sum_on'] += y_on.sum()
                    st['n_on'] += y_on.size

        self.sample_idx += n

    def result(self):
        channels = {}
        for label, st in self.stats.items():
            n_off = st['n_off']
            if n_off == 0:
                channels[label] = dict(
                    amplitude=0.0, phase_rad=0.0, complex_factor_re=0.0, complex_factor_im=0.0,
                    snr_db=0.0, quality=ChannelQualityStatus.POOR,
                    error='no gate-off samples captured',
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


class CalibrationAnalysisWorker(QThread):
    '''
    Drains one raw-IQ buffer from each device's calibration Rx queue per
    iteration and feeds it to a CalibrationAnalyzer. Assumes all configured
    devices produce matching per-call buffer shapes (same assumption
    SaveWorker already makes for its own multi-device buffers).
    '''
    logEvent = pyqtSignal(str, str)

    def __init__(self, analyzer: CalibrationAnalyzer, rx_queues: list, running: bool = True, parent=None):
        super().__init__(parent)
        self.analyzer = analyzer
        self.rx_queues = rx_queues
        self.running = running

    def run(self):
        self.logEvent.emit('debug', 'Calibration analysis started')
        while self.running:
            try:
                buffers = [q.get(timeout=0.5) for q in self.rx_queues]
            except queue.Empty:
                continue
            try:
                stacked = np.vstack(buffers)
                self.analyzer.add_chunk(stacked)
            except Exception as e:
                self.logEvent.emit('error', f'Calibration analysis error: {e}')
        self.logEvent.emit('debug', 'Calibration analysis stopped')

    def stop(self):
        self.running = False
