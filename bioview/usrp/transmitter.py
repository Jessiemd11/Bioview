import uhd 
import math 
import numpy as np 
from PyQt6.QtCore import QThread, pyqtSignal

from bioview.constants import INIT_DELAY
from bioview.types import UsrpConfiguration

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
            
        len_buf = 20 * self.tx_waveform_size
        
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
        
    def run(self): 
        self.logEvent.emit('debug', 'Transmission Started')
        tx_metadata = uhd.types.TXMetadata()
        tx_metadata.start_of_burst = True
        tx_metadata.end_of_burst = False
        tx_metadata.has_time_spec = True
        tx_metadata.time_spec = uhd.types.TimeSpec(self.usrp.get_time_now().get_real_secs() + INIT_DELAY)
            
        while self.running:
            # Check for updated parameters 
            curr_tx_gain = self.config.get_param_value('tx_gain')
            if curr_tx_gain != self.tx_gain: 
                for chan in self.config.tx_channels:
                    self.usrp.set_tx_gain(curr_tx_gain[chan], chan)
                self.logEvent.emit('debug', f'Tx gain updated to {curr_tx_gain}. Current {self.tx_gain}')
                self.tx_gain = curr_tx_gain
                
            try:
                # Send samples
                buffer_iter = self.tx_waveform
                num_samps = self.tx_streamer.send(buffer_iter, tx_metadata)
            except RuntimeError as ex:
                self.logEvent.emit('error', f'Runtime error in transmit: {ex}')
                continue
  
            # Continue transmission
            tx_metadata.start_of_burst = False 
            tx_metadata.has_time_spec  = False 
            
            if num_samps < self.tx_buffer_size:
                self.logEvent.emit('warning', f'Tx Sent only {num_samps} samples')
        
        # End transmission
        tx_metadata.end_of_burst = True
        self.tx_streamer.send(np.zeros_like(self.tx_waveform), tx_metadata)
        self.logEvent.emit('debug', 'Transmission Stopped')
    
    def stop(self):
        self.running = False