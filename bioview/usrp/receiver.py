import uhd 
import queue
import numpy as np
from PyQt6.QtCore import QThread, pyqtSignal

from bioview.constants import INIT_DELAY, SETTLING_TIME, SAVE_BUFFER_SIZE
from bioview.types import UsrpConfiguration

class ReceiveWorker(QThread):
    logEvent = pyqtSignal(str, str)

    def __init__(self, 
                 usrp, 
                 config: UsrpConfiguration, 
                 rx_streamer, 
                 rx_queue, 
                 running:bool = True,
                 parent = None
        ):
        super().__init__(parent)
        # Modifiable params
        self.rx_gain = config.get_param_value('rx_gain').copy()
        
        self.config = config 
        
        # Device params
        self.usrp = usrp
        self.rx_streamer = rx_streamer
        self.rx_queue = rx_queue
        self.running = running

    def run(self):
        self.logEvent.emit('debug', 'Receiving Started')
        if self.usrp is None or self.rx_streamer is None:
            self.logEvent.emit('error', 'USRP or Rx streamer not initialized.')
            return

        rx_metadata = uhd.types.RXMetadata() 
        
        # Buffer for receiving samples
        num_channels = self.rx_streamer.get_num_channels()
        max_samps_per_packet = self.rx_streamer.get_max_num_samps()
        
        recv_buffer_size = max_samps_per_packet * SAVE_BUFFER_SIZE # Make receive buffer larger than max_samps_per_packet
        
        recv_buffer = np.empty((num_channels, recv_buffer_size), dtype=np.complex64)
        
        # Setup streaming using continuous saving mode by default
        stream_cmd = uhd.types.StreamCMD(uhd.types.StreamMode.start_cont)
        
        # When using multiple devices, we need to set stream_now to False to align time edges of packets  
        stream_cmd.stream_now = False
        stream_cmd.time_spec = uhd.types.TimeSpec(self.usrp.get_time_now().get_real_secs() + INIT_DELAY)
        self.rx_streamer.issue_stream_cmd(stream_cmd)
        
        # Initialize 
        total_samps_received = 0 
        timeout = 0.5 # Larger timeout initially
        had_an_overflow = False
        last_overflow = uhd.types.TimeSpec(0)
    
        # Setup the statistic counters
        num_rx_samps = 0
        num_rx_dropped = 0
        
        rate = self.usrp.get_rx_rate()

        while self.running:
            # Check for updated parameters 
            curr_rx_gain = self.config.get_param_value('rx_gain')
            if curr_rx_gain != self.rx_gain: 
                for chan in self.config.rx_channels:
                    self.usrp.set_rx_gain(curr_rx_gain[chan], chan)
                self.logEvent.emit('debug', f'Rx gain updated to {curr_rx_gain}. Current {self.rx_gain}')
                self.rx_gain = curr_rx_gain
            
            try:
                # Receive samples
                num_rx_samps = self.rx_streamer.recv(recv_buffer, rx_metadata, timeout)
            except RuntimeError as ex:
                self.logEvent.emit('error', f'Receiver Runtime Eror: {ex}')
                continue

            timeout = INIT_DELAY # Reduce timeout for subsequent transmissions
        
            # Reference: uhd/examples/python/benchmark_rate.py
            # Handle the error codes
            if rx_metadata.error_code == uhd.types.RXMetadataErrorCode.none:
                # Reset the overflow flag
                if had_an_overflow:
                    had_an_overflow = False
                    num_rx_dropped += (rx_metadata.time_spec - last_overflow).to_ticks(rate)
            elif rx_metadata.error_code == uhd.types.RXMetadataErrorCode.overflow:
                had_an_overflow = True
                # Need to make sure that last_overflow is a new TimeSpec object, not
                # a reference to metadata.time_spec, or it would not be useful
                # further up.
                last_overflow = uhd.types.TimeSpec(
                    rx_metadata.time_spec.get_full_secs(), rx_metadata.time_spec.get_frac_secs()
                )
                self.logEvent.emit('warning', f'Receiver Overflow: {rx_metadata.strerror()}')
            elif rx_metadata.error_code == uhd.types.RXMetadataErrorCode.late:
                self.logEvent.emit('warning', f'Receiver Late: {rx_metadata.strerror()}, restarting...')
                # Radio core will be in the idle state. Issue stream command to restart streaming.
                stream_cmd.time_spec = uhd.types.TimeSpec(
                    self.usrp.get_time_now().get_real_secs() + INIT_DELAY
                )
                stream_cmd.stream_now = num_channels == 1
                self.rx_streamer.issue_stream_cmd(stream_cmd)
            elif rx_metadata.error_code == uhd.types.RXMetadataErrorCode.timeout:
                self.logEvent.emit('warning', f'Receiver Timeout: {rx_metadata.strerror()}')
            else:
                self.logEvent.emit('warning', f'Receiver Error: {rx_metadata.strerror()}')

            total_samps_received += num_rx_samps
            
            # Copy samples to avoid buffer overwrite and put in queue
            # recv_buffer.dtype = np.complex64 (since default cpu_format = 'fc32')
            try:
                self.rx_queue.put((recv_buffer))
            except queue.Full:
                self.logEvent.emit('warning', 'Rx Queue full, dropping buffer')
            except queue.Empty: 
                self.logEvent.emit('debug', 'Rx Queue Empty')
                continue
                
        # Gracefully close once receiving is finished
        stream_cmd = uhd.types.StreamCMD(uhd.types.StreamMode.stop_cont)
        self.rx_streamer.issue_stream_cmd(stream_cmd)
        self.logEvent.emit('debug', 'Receiving Stopped')
        
    def stop(self):
        self.running = False