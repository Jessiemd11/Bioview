import uhd 
import math 
import time
import numpy as np 
from PyQt6.QtCore import QThread, pyqtSignal

from bioview.constants import INIT_DELAY
from bioview.types import UsrpConfiguration
from bioview.utils import send_all, drain_tx_async_msgs, sample_index_at, time_spec_from_sample

# Length of waveform handed to each send() call. A short buffer (it used to be
# ~7 ms) means this thread has to win the GIL every few ms against the Save /
# GUI threads; any longer stall empties the device FIFO and the carrier drops
# out (Tx underflow). ~150 ms per call leaves plenty of slack.
TX_BUFFER_S = 0.15
UNDERFLOW_REPORT_S = 10.0
# After an underflow, restart the burst this far in the future - must be later
# than the samples still queued in the host/device buffers (~2 buffers), or
# the restart itself would arrive late.
TX_RESTART_DELAY_S = 0.5
# Underflow reports that arrive this soon after a restart belong to the same
# incident - don't restart again for them
TX_RESTART_HOLDOFF_S = TX_RESTART_DELAY_S + 0.3
TX_RESTART_WARN_COUNT = 3

class TransmitWorker(QThread):
    logEvent = pyqtSignal(str, str)
    
    def __init__(self, 
                 config: UsrpConfiguration, 
                 usrp, 
                 tx_streamer, 
                 running: bool = True, 
                 parent = None
        ):
        super().__init__(parent)
        # Modifiable params
        self.tx_gain = config.get_param_value('tx_gain').copy()
        self.tx_amplitude = config.get_param_value('tx_amplitude')
        
        # Fixed params
        self.samp_rate = config.get_param_value('samp_rate')
        self.if_freq = config.get_param_value('if_freq')
        self.tx_channels = config.get_param_value('tx_channels')
        
        self._generate_tx_waveforms()    
        
        self.config = config
        self.usrp = usrp
        self.tx_streamer = tx_streamer
        self.running = running
        self.tx_buffer_size = self.tx_streamer.get_max_num_samps() 
    
    def _generate_tx_waveforms(self):
        '''
        Generate sine waves for each Tx channel, using as minimum a buffer size as possible.
        The buffer is made larger in length to be able to read circularly without causing overflow issues 
        '''
        get_buf_size = lambda x: int(self.samp_rate * x / (math.gcd(int(self.samp_rate), int(x))**2))
        get_lcm = lambda a, b: int(a * b / math.gcd(int(a), int(b)))
        
        if len(self.if_freq) == 1: 
            self.tx_waveform_size = get_buf_size(self.if_freq[0])
        else: 
            # Return the least common multiple
            self.tx_waveform_size = get_lcm(get_buf_size(self.if_freq[0]), get_buf_size(self.if_freq[1]))
            
        # Whole number of waveform periods, so the buffer still loops seamlessly
        num_periods = max(1, math.ceil(TX_BUFFER_S * self.samp_rate / self.tx_waveform_size))
        len_buf = num_periods * self.tx_waveform_size
        
        self.tx_waveform = np.zeros((len(self.tx_channels), len_buf), dtype=np.complex64)
        
        # Generate IQ Modulated IF signals
        for idx, _ in enumerate(self.tx_channels):
            self.tx_waveform[idx] = uhd.dsp.signals.get_continuous_tone(
                self.samp_rate,
                self.if_freq[idx],
                self.tx_amplitude[idx],
                desired_size=len_buf,
                max_size=(2 * self.samp_rate),
                waveform='sine',
            )
        
    def _begin_burst(self, delay):
        """ Timed start of a new burst. The waveform is phase-locked to device
        time: device sample n always carries tx_waveform[:, n % L] (the buffer
        is a whole number of periods of every IF tone). So however long Tx was
        interrupted, the tone resumes with the phase it would have had - no
        lasting phase step in the received signal. """
        n = sample_index_at(self.usrp.get_time_now().get_real_secs() + delay, self.samp_rate)
        md = uhd.types.TXMetadata()
        md.start_of_burst = True
        md.end_of_burst = False
        md.has_time_spec = True
        md.time_spec = time_spec_from_sample(n, self.samp_rate)
        return md, n, n % self.tx_waveform.shape[1]

    def _end_burst(self):
        md = uhd.types.TXMetadata()
        md.end_of_burst = True
        self.tx_streamer.send(np.zeros((len(self.tx_channels), 1), dtype=np.complex64), md)

    def run(self): 
        self.logEvent.emit('debug', 'Transmission Started')
        tx_metadata, _, pos = self._begin_burst(INIT_DELAY)

        async_md = uhd.types.TXAsyncMetadata()
        underflows = 0
        last_report = time.monotonic()
        last_restart = -math.inf
        recent_restarts = []

        while self.running:
            # Check for updated parameters 
            curr_tx_gain = self.config.get_param_value('tx_gain')
            if curr_tx_gain != self.tx_gain: 
                for chan in self.config.tx_channels:
                    self.usrp.set_tx_gain(curr_tx_gain[chan], chan)
                self.logEvent.emit('debug', f'Tx gain updated to {curr_tx_gain}. Current {self.tx_gain}')
                self.tx_gain = curr_tx_gain

            # Continue from wherever the device-time phase lock says we are
            buf = self.tx_waveform if pos == 0 else np.ascontiguousarray(self.tx_waveform[:, pos:])
            try:
                # Send the whole buffer (resending any remainder on a partial
                # send keeps the tone phase-continuous)
                num_samps = send_all(self.tx_streamer, buf, tx_metadata, lambda: self.running)
            except RuntimeError as ex:
                self.logEvent.emit('error', f'Runtime error in transmit: {ex}')
                continue
            pos = (pos + num_samps) % self.tx_waveform.shape[1]
            if not self.running:
                break

            stalled = num_samps < buf.shape[1]
            if stalled:
                self.logEvent.emit('warning', f'Tx sent only {num_samps} of {buf.shape[1]} samples')

            new_underflows, errors = drain_tx_async_msgs(self.tx_streamer, async_md)
            underflows += new_underflows
            now = time.monotonic()
            in_holdoff = now - last_restart < TX_RESTART_HOLDOFF_S
            if stalled or errors or (new_underflows and not in_holdoff):
                # The device carries on with the next queued samples after an
                # underflow, which shifts the tone phase by 2*pi*f_IF*gap for
                # the rest of the recording. End the burst and restart it at a
                # timed, phase-locked position instead.
                self._end_burst()
                tx_metadata, n, pos = self._begin_burst(TX_RESTART_DELAY_S)
                last_restart = now
                cause = f'{new_underflows} underflow(s)' if new_underflows else ('stalled send' if stalled else f'{errors} late/sequence error(s)')
                self.logEvent.emit('warning', f'Tx restarted after {cause}; resumes in phase at device time {n / self.samp_rate:.3f} s')
                recent_restarts = [t for t in recent_restarts if now - t < UNDERFLOW_REPORT_S] + [now]
                if len(recent_restarts) > TX_RESTART_WARN_COUNT:
                    self.logEvent.emit('warning', f'Tx restarted {len(recent_restarts)} times in {UNDERFLOW_REPORT_S:.0f} s - the PC is not keeping up (other apps / power saving?)')

            if now - last_report >= UNDERFLOW_REPORT_S:
                if underflows:
                    self.logEvent.emit('warning', f'Tx underflows: {underflows} in last {now - last_report:.0f} s (host too slow - carrier dropped out)')
                underflows = 0
                last_report = now
        
        # End transmission
        self._end_burst()
        self.logEvent.emit('debug', 'Transmission Stopped')
    
    def stop(self):
        self.running = False