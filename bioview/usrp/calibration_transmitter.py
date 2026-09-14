import uhd
import time
import numpy as np
from PyQt6.QtCore import QThread, pyqtSignal

from bioview.constants import INIT_DELAY
from bioview.types import UsrpConfiguration
from bioview.utils import TriangleGenerator

# Fixed calibration injection parameters (matches the standalone B210 calibration script)
TRI_FREQ_HZ = 10.0
NUM_TRIANGLES = 5
INJ_PERIOD_S = 1.0
TRI_AMP = 0.5

class CalibrationTransmitWorker(QThread):
    '''
    Transmits gated triangle-wave-modulated IF tones (carrier * (1 + triangle))
    on every Tx channel for duration_s seconds, then stops itself.

    The waveform for exactly one injection period is precomputed once in
    __init__ (like UsrpTransmitter._generate_tx_waveforms()) and the same
    buffer is resent every iteration, instead of regenerating a chunk from
    scratch on every send - per-chunk regeneration (TriangleGenerator +
    np.exp() every ~4ms) was heavy enough to starve the concurrent RX
    thread of CPU/GIL time and cause device-side RX FIFO overflows.
    A small, fixed phase discontinuity can occur at the once-per-period
    buffer wraparound if if_freq * period_len / samp_rate isn't an exact
    integer, but that always lands at the very start of the next gate burst
    (pos == 0), which CalibrationAnalyzer's guard band around every on/off
    transition already excludes from the gate-off reference measurement.
    '''
    logEvent = pyqtSignal(str, str)
    finished = pyqtSignal()

    def __init__(self,
                 config: UsrpConfiguration,
                 usrp,
                 tx_streamer,
                 duration_s: float = 30.0,
                 running: bool = True,
                 parent=None
        ):
        super().__init__(parent)
        self.config = config
        self.usrp = usrp
        self.tx_streamer = tx_streamer
        self.duration_s = float(duration_s)
        self.running = running

        self.samp_rate = config.get_param_value('samp_rate')
        self.if_freq = config.get_param_value('if_freq')
        self.tx_channels = config.get_param_value('tx_channels')
        # Halve the carrier amplitude so (1 + triangle) * amplitude (max ~1.5x)
        # stays under the streamer's ~1.0 headroom
        base_amp = config.get_param_value('tx_amplitude')
        self.tx_amplitude = [0.5 * a for a in base_amp]

        self.period_len = max(1, int(round(INJ_PERIOD_S * self.samp_rate)))
        self.tx_waveform = self._generate_waveform()

    def _generate_waveform(self):
        ''' One full injection period, computed once. '''
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
            buf[idx] = (carrier * (1.0 + tri) * self.tx_amplitude[idx]).astype(np.complex64)

        return buf

    def run(self):
        self.logEvent.emit('debug', 'Calibration transmission started')
        tx_metadata = uhd.types.TXMetadata()
        tx_metadata.start_of_burst = True
        tx_metadata.end_of_burst = False
        tx_metadata.has_time_spec = True
        tx_metadata.time_spec = uhd.types.TimeSpec(self.usrp.get_time_now().get_real_secs() + INIT_DELAY)

        start_time = time.monotonic()
        while self.running and (time.monotonic() - start_time) < self.duration_s:
            try:
                self.tx_streamer.send(self.tx_waveform, tx_metadata)
            except RuntimeError as ex:
                self.logEvent.emit('error', f'Runtime error in calibration transmit: {ex}')
                continue

            tx_metadata.start_of_burst = False
            tx_metadata.has_time_spec = False

        # End transmission
        tx_metadata.end_of_burst = True
        self.tx_streamer.send(np.zeros((len(self.tx_channels), 1), dtype=np.complex64), tx_metadata)
        self.logEvent.emit('debug', 'Calibration transmission stopped')
        self.finished.emit()

    def stop(self):
        self.running = False
