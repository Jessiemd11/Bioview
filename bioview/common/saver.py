import queue
import numpy as np

from PyQt6.QtCore import QThread, pyqtSignal

from bioview.utils import init_save_file, update_save_file, get_filter, apply_filter
from bioview.constants import SAVE_BUFFER_SIZE
from bioview.types import UsrpConfiguration, ExperimentConfiguration

class SaveWorker(QThread):
    data_ready = pyqtSignal(dict)
    logEvent = pyqtSignal(str, str)

    def __init__(self, 
                 exp_config: ExperimentConfiguration, 
                 usrp_config: list[UsrpConfiguration],
                 rx_queues: list[queue.Queue], 
                 disp_queue: queue.Queue, 
                 running: bool = True,
                 saving: bool = True, 
                 save_iq: bool = True,
                 buffer_size: int = 2
        ):
        super().__init__()
        self.usrp_config = usrp_config
        self.exp_config = exp_config        
        self.rx_queues = rx_queues
        self.disp_queue = disp_queue
        
        self.running = running
        self.saving = saving 
        
        # Store a few elements in buffer before adding
        self.buffer_size = buffer_size
        
        # Allow for saving either IQ or Amp/Phase (default)
        self.save_iq = save_iq
        
        # Load IF filters
        self.if_filts = [self._load_filter(freq) for freq in exp_config.channel_ifs]
        
        # Load output file
        self.out_file = exp_config.get_save_path() 
        if self.exp_config.save_phase:
            num_channels = 2 * len(exp_config.data_mapping)
        else:
            num_channels = len(exp_config.data_mapping)
        if self.saving:
            init_save_file(file_path = self.out_file, 
                           num_channels = num_channels, 
                           chunk_size=500)
        
        # Initialize states for all valid declared channel combinations
        self.phase_accumulator = {}
        self.filter_states = {}
        for ch_key in self.exp_config.data_mapping.keys():
            self.phase_accumulator[ch_key] = 0.0
            self.filter_states[ch_key] = None            

    def _load_filter(self, freq: float, order: int = 2): 
        bandwidth = self.exp_config.if_filter_bw
        low_cutoff = freq - bandwidth / 2
        high_cutoff = freq + bandwidth / 2
        
        filter = get_filter(bounds=[low_cutoff, high_cutoff], 
                            samp_rate=self.exp_config.samp_rate,
                            btype='band', order=order)
        return filter
    
    def _process_chunk(self, 
                       data, 
                       filter, 
                       if_freq, 
                       channel_key
        ):
        # Early return for empty data
        if len(data) == 0:
            return np.array([]), np.array([])
        
        # Store last sample for continuity checking
        if hasattr(self, 'last_samples') and channel_key in self.last_samples:
            # Check for significant discontinuity
            discontinuity = abs(data[0] - self.last_samples[channel_key])
            if discontinuity > 3 * np.std(data[:min(100, len(data))]):
                self.logEvent.emit('debug', f'Potential discontinuity detected in {channel_key}')
        
        # Store last sample for next buffer
        if not hasattr(self, 'last_samples'):
            self.last_samples = {}
        self.last_samples[channel_key] = data[-1]
        
        # Stateful filtering
        current_filter_state = self.filter_states.get(channel_key)
        filt_data, new_filter_state = apply_filter(data, filter, zi=current_filter_state)
        self.filter_states[channel_key] = new_filter_state
        
        # Get the current accumulated phase for this channel
        current_phase = self.phase_accumulator[channel_key]
        
        # Get phase for all samples
        phase_increment = 2 * np.pi * if_freq / self.exp_config.samp_rate
        phases = current_phase + np.arange(len(filt_data)) * phase_increment
        
        # Down-convert from IF to baseband with phase continuity
        downconversion = np.exp(-1j * phases)
        baseband_data = filt_data * downconversion 
        
        # Update phase accumulator for next buffer (mod 2π to prevent numerical drift)
        self.phase_accumulator[channel_key] = (phases[-1] + phase_increment)
        
        # Downsampling logic
        step = self.exp_config.save_ds
        end_idx = len(baseband_data) - step + 1
        num_windows = (end_idx + step - 1) // step  # Calculate the number of windows

        if num_windows <= 0:
            return np.array([]), np.array([])

        # Create indices for the start of each window
        start_indices = np.arange(0, end_idx, step)
        
        # Use advanced indexing to get all the windows
        window_indices = start_indices[:, np.newaxis] + np.arange(step)
        windows = baseband_data[window_indices]  

        if self.save_iq:
            first_comp = np.mean(np.real(windows), axis=1)
            second_comp = np.mean(np.imag(windows), axis=1)
        else:
            first_comp = np.mean(np.abs(windows), axis=1)
            second_comp = np.mean(np.angle(windows, True), axis=1)

        return first_comp, second_comp
    
    def _process(self, buffer):
        # Use numpy preallocated array for speed 
        num_channels = len(self.exp_config.data_mapping)
        save_list = np.empty((num_channels, int(buffer.shape[1] // self.exp_config.save_ds), 2))
        
        for r_idx, row in enumerate(self.exp_config.channel_mapping):
            x = buffer[r_idx, :]
            for t_idx, channel_key in enumerate(row): 
                channel_idx = self.exp_config.data_mapping[channel_key]
                # Pass the channel key for state tracking
                first_comp, second_comp = self._process_chunk(
                    data = x, 
                    filter = self.if_filts[t_idx], 
                    if_freq = self.exp_config.channel_ifs[t_idx], 
                    channel_key = channel_key
                )
                self.logEvent.emit('debug', f'Processed channel {channel_key} with index {channel_idx}')
                save_list[channel_idx, :, 0] = first_comp
                save_list[channel_idx, :, 1] = second_comp
        
        # Return all processed samples
        return save_list
    
    def run(self):
        self.logEvent.emit('debug', 'Saving started')
        
        # Preallocate empty buffer to get
        data_buf = []
        samples = [None] * len(self.rx_queues)
        
        while self.running:
            try:
                # Get from all queues
                if len(data_buf) < self.buffer_size: 
                    for idx, rx_q in enumerate(self.rx_queues): 
                        samples[idx] = rx_q.get()
                    data_buf.append(np.transpose(np.vstack(samples)))    
                else: 
                    # TODO: Correctly assign shapes
                    self.logEvent.emit('debug', f'Buffer size: {len(data_buf)} with elem shape {data_buf[0].shape}')
                    buffer_data = np.transpose(np.vstack(data_buf))
                    processed = self._process(buffer_data) 
                    self.logEvent.emit('debug', f'Processed data shape {processed.shape}')   
                    
                    # Add to display queue 
                    try: 
                        self.disp_queue.put(processed)
                    except queue.Empty: 
                        self.logEvent.emit('debug', 'Display Queue Empty')    
                    except queue.Full: 
                        self.logEvent.emit('debug', 'Display Queue Full')
                    
                    # Add to save queue as well (asynchronously save using save queue if performance is an issue)
                    
                    # Write to file, only if saving 
                    if self.saving:
                        update_save_file(self.out_file, processed)
                    
                    # Clear buffer 
                    data_buf = []
            except queue.Empty:
                self.logEvent.emit('debug', 'Rx Queue Empty')
                continue
            except queue.Full: 
                self.logEvent.emit('debug', 'Rx Queue Full')
                continue
            except Exception as e:
                self.logEvent.emit('error', f'Saving error: {e}')
                continue
                
        self.logEvent.emit('debug', 'Saving stopped')
        
    def stop(self):
        self.running = False