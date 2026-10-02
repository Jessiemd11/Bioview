import uhd 
import time
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

        # Cumulative samples (per channel) put on rx_queue - lets the app mark
        # a point in the stream and tell when the SaveWorker has got that far
        self.total_samps_received = 0

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
        
        # The streamer is shared across recordings - drop anything a previous
        # run left behind (e.g. one that ended abnormally) before starting
        self._flush(recv_buffer, rx_metadata)

        # Setup streaming using continuous saving mode by default
        stream_cmd = uhd.types.StreamCMD(uhd.types.StreamMode.start_cont)
        
        # When using multiple devices, we need to set stream_now to False to align time edges of packets  
        stream_cmd.stream_now = False
        stream_cmd.time_spec = uhd.types.TimeSpec(self.usrp.get_time_now().get_real_secs() + INIT_DELAY)
        start_secs = stream_cmd.time_spec.get_real_secs()
        self.rx_streamer.issue_stream_cmd(stream_cmd)
        
        # Initialize 
        # Larger timeout until the stream has actually started (start delay +
        # device/USB latency is well over INIT_DELAY)
        timeout = 0.5
        started = False
        start_wait_began = time.monotonic()
        start_wait_warned = False
        stale_logged = False
        had_an_overflow = False
        last_overflow = uhd.types.TimeSpec(0)
    
        # Setup the statistic counters
        num_rx_samps = 0
        num_rx_dropped = 0
        
        rate = self.usrp.get_rx_rate()
        fs_int = int(round(rate))
        next_n = None   # device sample index expected at the start of the next buffer

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

            if not started:
                if num_rx_samps == 0:
                    if rx_metadata.error_code == uhd.types.RXMetadataErrorCode.timeout:
                        # Still waiting for the timed start - not an error yet
                        if not start_wait_warned and time.monotonic() - start_wait_began > 2.0:
                            self.logEvent.emit('warning', 'Receiver: no samples 2 s after start')
                            start_wait_warned = True
                        continue
                    # Anything else (e.g. late command) is handled below
                elif rx_metadata.time_spec.get_real_secs() < start_secs - 1e-6:
                    # Samples from before this stream's start time (left over
                    # from a previous run) - don't record them
                    if not stale_logged:
                        self.logEvent.emit('debug', 'Receiver: discarding stale samples from a previous stream')
                        stale_logged = True
                    continue
                else:
                    started = True
                    timeout = INIT_DELAY # Reduce timeout once the stream is flowing
        
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
                start_secs = stream_cmd.time_spec.get_real_secs()
                self.rx_streamer.issue_stream_cmd(stream_cmd)
            elif rx_metadata.error_code == uhd.types.RXMetadataErrorCode.timeout:
                self.logEvent.emit('warning', f'Receiver Timeout: {rx_metadata.strerror()}')
            else:
                self.logEvent.emit('warning', f'Receiver Error: {rx_metadata.strerror()}')

            # Copy only the samples actually written this call, and copy them
            # out of recv_buffer before the next recv() overwrites it in place.
            # recv_buffer.dtype = np.complex64 (since default cpu_format = 'fc32')
            if num_rx_samps > 0:
                rx_data = recv_buffer[:, :num_rx_samps].copy()

                # Detect dropped samples (overflow) from the timestamps, so the
                # SaveWorker can keep its downconversion phase on device time
                ts = rx_metadata.time_spec
                first_n = ts.get_full_secs() * fs_int + int(round(ts.get_frac_secs() * rate))
                gap = 0 if next_n is None else first_n - next_n
                if gap > 0:
                    self.logEvent.emit('warning', f'Rx dropped {gap} samples ({gap / rate * 1e3:.1f} ms) at device time {first_n / rate:.3f} s')
                gap = max(gap, 0)
                next_n = first_n + num_rx_samps
                try:
                    self.rx_queue.put((rx_data, first_n, gap))
                    self.total_samps_received += num_rx_samps
                except queue.Full:
                    self.logEvent.emit('warning', 'Rx Queue full, dropping buffer')
                except queue.Empty:
                    self.logEvent.emit('debug', 'Rx Queue Empty')
                    continue
                
        # Gracefully close once receiving is finished
        stream_cmd = uhd.types.StreamCMD(uhd.types.StreamMode.stop_cont)
        self.rx_streamer.issue_stream_cmd(stream_cmd)
        # Read out what was already in flight, so the next recording on this
        # streamer doesn't start with this one's tail
        self._flush(recv_buffer, rx_metadata)
        self.logEvent.emit('debug', 'Receiving Stopped')

    def _flush(self, recv_buffer, rx_metadata, max_s: float = 1.0):
        deadline = time.monotonic() + max_s
        while time.monotonic() < deadline:
            try:
                if self.rx_streamer.recv(recv_buffer, rx_metadata, 0.1) == 0:
                    break
            except RuntimeError:
                break
        
    def stop(self):
        self.running = False