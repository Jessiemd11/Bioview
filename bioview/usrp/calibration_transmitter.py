import uhd
import numpy as np
from PyQt6.QtCore import QThread, pyqtSignal

from bioview.constants import INIT_DELAY
from bioview.types import UsrpConfiguration
from bioview.utils import TriangleGenerator, send_all

# Fixed calibration injection parameters, tuned for this experiment protocol:
# Atri=0.5, ftri=2 kHz, Tburst=2.5 ms, Trpt=1 s. Changing TRI_FREQ_HZ/
# NUM_TRIANGLES also changes what save_rate and if_filter_bw are required to
# resolve the probe - see CalibrationAnalyzer._check_rates()
# (bioview/common/calibrator.py), which is the single source of truth for
# those requirements; don't restate the derived numbers here, they will drift.
TRI_FREQ_HZ = 2000.0
NUM_TRIANGLES = 5
INJ_PERIOD_S = 1.0
TRI_AMP = 0.5
# Unmodulated carrier sent before the first period so that *every* burst,
# including the first, has a stable carrier both for its reference/pre-burst
# window (CalibrationAnalyzer's h_amp, which needs MAX_LAG_S of carrier ahead of
# the burst) and for the IF/baseband filters to have settled after Tx turn-on.
# Must stay comfortably above MAX_LAG_S (50 ms) plus filter settling.
LEAD_IN_S = 0.1

class CalibrationTransmitWorker(QThread):
    '''
    Transmits gated triangle-wave-modulated IF tones (carrier * (1 + triangle))
    for num_cycles * len(tx_channels) periods, then stops itself.

    Time-division multiplexed across Tx channels: only one Tx channel carries
    the triangle modulation during any given INJ_PERIOD_S period (the others
    transmit an unmodulated carrier, tri=0), cycling round-robin one channel
    per period so every Tx channel gets exactly num_cycles bursts of its own.
    This keeps cross-coupling channels (e.g. Tx2Rx1) from being evaluated
    while Tx1 is *also* actively modulating, so any imperfect IF isolation
    doesn't show up as contamination on top of genuine coupling.
    CalibrationAnalyzer determines which Tx was active for a given burst by
    which direct-diagonal channel actually shows a resolved triangle that
    burst, rather than assuming a fixed period-count parity - see
    CalibrationAnalyzer._finalize_burst.

    One waveform buffer per Tx-channel role (i.e. "channel idx N is the
    active one this period") is precomputed once in __init__ (like
    UsrpTransmitter._generate_tx_waveforms()) and cycled through on each
    send(), instead of regenerating a chunk from scratch on every send -
    per-chunk regeneration (TriangleGenerator + np.exp() every ~4ms) was
    heavy enough to starve the concurrent RX thread of CPU/GIL time and cause
    device-side RX FIFO overflows.
    A small, fixed phase discontinuity can occur at the once-per-period
    buffer wraparound if if_freq * period_len / samp_rate isn't an exact
    integer, but that always lands at the very start of the next gate burst
    (pos == 0), which CalibrationAnalyzer's guard band around every on/off
    transition already excludes from the gate-off reference measurement.

    Intended usage: run this for a short (num_cycles-bounded) episode at the
    start and end of a normal recording, on the same tx_streamer the normal
    UsrpTransmitter uses for that device - never concurrently with it, since
    only one thread should own a given tx_streamer's send() calls at a time.
    '''
    logEvent = pyqtSignal(str, str)
    finished = pyqtSignal()

    def __init__(self,
                 config: UsrpConfiguration,
                 usrp,
                 tx_streamer,
                 num_cycles: int = 3,
                 running: bool = True,
                 parent=None
        ):
        super().__init__(parent)
        self.config = config
        self.usrp = usrp
        self.tx_streamer = tx_streamer
        self.running = running

        self.samp_rate = config.get_param_value('samp_rate')
        self.if_freq = config.get_param_value('if_freq')
        self.tx_channels = config.get_param_value('tx_channels')
        # Halve the carrier amplitude so (1 + triangle) * amplitude (max ~1.5x)
        # stays under the streamer's ~1.0 headroom
        base_amp = config.get_param_value('tx_amplitude')
        self.tx_amplitude = [0.5 * a for a in base_amp]

        self.total_periods = max(1, int(num_cycles)) * len(self.tx_channels)

        self.period_len = max(1, int(round(INJ_PERIOD_S * self.samp_rate)))
        # One waveform buffer per Tx-channel role: tx_waveforms[k] has channel
        # k triangle-modulated and every other channel on an unmodulated carrier.
        self.tx_waveforms = [self._generate_waveform(active_idx)
                             for active_idx in range(len(self.tx_channels))]
        self.lead_in = self._generate_lead_in()

    def _generate_waveform(self, active_idx: int):
        ''' One full injection period, computed once, for a single active-Tx role. '''
        tri_gen = TriangleGenerator(
            fs=self.samp_rate,
            freq=TRI_FREQ_HZ,
            n_tri=NUM_TRIANGLES,
            period_s=INJ_PERIOD_S,
            amplitude=TRI_AMP,
        )
        t = np.arange(self.period_len, dtype=np.float64) / self.samp_rate
        tri, _ = tri_gen.next(self.period_len)

        buf = np.empty((len(self.tx_channels), self.period_len), dtype=np.complex64)
        for idx in range(len(self.tx_channels)):
            carrier = np.exp(1j * 2 * np.pi * self.if_freq[idx] * t)
            modulation = (1.0 + tri) if idx == active_idx else 1.0
            buf[idx] = (carrier * modulation * self.tx_amplitude[idx]).astype(np.complex64)

        return buf

    def _generate_lead_in(self):
        ''' Unmodulated carrier on every channel, ending at phase 0 so it joins
        _generate_waveform()'s t=0 start with continuous phase for any if_freq. '''
        n = max(1, int(round(LEAD_IN_S * self.samp_rate)))
        t = np.arange(-n, 0, dtype=np.float64) / self.samp_rate
        buf = np.empty((len(self.tx_channels), n), dtype=np.complex64)
        for idx in range(len(self.tx_channels)):
            buf[idx] = (np.exp(1j * 2 * np.pi * self.if_freq[idx] * t)
                        * self.tx_amplitude[idx]).astype(np.complex64)
        return buf

    def run(self):
        self.logEvent.emit('debug', 'Calibration transmission started')
        tx_metadata = uhd.types.TXMetadata()
        tx_metadata.start_of_burst = True
        tx_metadata.end_of_burst = False
        tx_metadata.has_time_spec = True
        tx_metadata.time_spec = uhd.types.TimeSpec(self.usrp.get_time_now().get_real_secs() + INIT_DELAY)

        try:
            send_all(self.tx_streamer, self.lead_in, tx_metadata, lambda: self.running)
        except RuntimeError as ex:
            # Flags stay set, so the first period's send() below still opens the burst
            self.logEvent.emit('error', f'Runtime error in calibration lead-in: {ex}')

        period_idx = 0
        while self.running and period_idx < self.total_periods:
            waveform = self.tx_waveforms[period_idx % len(self.tx_waveforms)]
            try:
                sent = send_all(self.tx_streamer, waveform, tx_metadata, lambda: self.running)
            except RuntimeError as ex:
                self.logEvent.emit('error', f'Runtime error in calibration transmit: {ex}')
                continue

            if sent < waveform.shape[1] and self.running:
                self.logEvent.emit('warning', f'Calibration Tx sent only {sent} of {waveform.shape[1]} samples')
            period_idx += 1

        # End transmission
        tx_metadata.end_of_burst = True
        self.tx_streamer.send(np.zeros((len(self.tx_channels), 1), dtype=np.complex64), tx_metadata)
        self.logEvent.emit('debug', 'Calibration transmission stopped')
        self.finished.emit()

    def stop(self):
        self.running = False
