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
CHUNK = 4096

class CalibrationTransmitWorker(QThread):
    '''
    Transmits gated triangle-wave-modulated IF tones (carrier * (1 + triangle))
    on every Tx channel for duration_s seconds, then stops itself. Unlike
    UsrpTransmitter, chunks are generated on the fly (the waveform evolves
    with the gate) rather than replaying one precomputed buffer.
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

        self.tri = TriangleGenerator(
            fs=self.samp_rate,
            freq=TRI_FREQ_HZ,
            n_tri=NUM_TRIANGLES,
            period_s=INJ_PERIOD_S,
            amplitude=TRI_AMP,
        )
        self.n = 0

    def _next_chunk(self, n):
        t = (self.n + np.arange(n, dtype=np.float64)) / self.samp_rate
        tri, _ = self.tri.next(n)

        buf = np.empty((len(self.tx_channels), n), dtype=np.complex64)
        for idx in range(len(self.tx_channels)):
            carrier = np.exp(1j * 2 * np.pi * self.if_freq[idx] * t)
            buf[idx] = (carrier * (1.0 + tri) * self.tx_amplitude[idx]).astype(np.complex64)

        self.n += n
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
                buf = self._next_chunk(CHUNK)
                self.tx_streamer.send(buf, tx_metadata)
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
