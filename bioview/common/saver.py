import time
import queue
import h5py
import numpy as np

from PyQt6.QtCore import QThread, pyqtSignal

from bioview.utils import init_save_file, append_save_chunk, get_filter, apply_filter
from bioview.constants import SAVE_BUFFER_SIZE
from bioview.types import UsrpConfiguration, ExperimentConfiguration

# Warn when the Rx queues hold more than this much unprocessed signal
BACKLOG_WARN_S = 1.0
BACKLOG_CHECK_S = 5.0

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

        # Cumulative raw (per channel) samples processed - same count as
        # ReceiveWorker.total_samps_received, so the app can tell when data
        # queued at some point in time has made it through (calibration)
        self.samples_processed = 0
        
        # Allow for saving either IQ or Amp/Phase (default)
        self.save_iq = save_iq
        
        # Load IF filters
        self.if_filts = [self._load_filter(freq) for freq in exp_config.channel_ifs]
        # Baseband anti-alias LPF, applied after downconversion and before
        # decimation - one per Tx/IF column, same indexing as if_filts
        self.baseband_filts = [self._load_baseband_filter() for _ in exp_config.channel_ifs]

        # Two-stage decimation: stage 1 (ds1) gives the calibration-rate
        # stream fed to data_ready; stage 2 (ds2) decimates that further to
        # save_ds for the file and display, which don't need the probe's
        # harmonics and would otherwise be far larger than necessary
        self.ds1 = exp_config.analysis_ds()
        self.ds2 = int(exp_config.save_ds) // self.ds1
        self.save_filt = self._load_save_filter() if self.ds2 > 1 else None

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
        self.baseband_filter_states = {}
        self.decim_remainder = {}
        self.save_filter_states = {}
        self.decim2_remainder = {}
        for ch_key in self.exp_config.data_mapping.keys():
            self.phase_accumulator[ch_key] = 0.0
            self.filter_states[ch_key] = None
            self.baseband_filter_states[ch_key] = None
            self.decim_remainder[ch_key] = np.zeros(0, dtype=complex)
            self.save_filter_states[ch_key] = None
            self.decim2_remainder[ch_key] = np.zeros(0, dtype=complex)

    def _load_filter(self, freq: float, order: int = 4):
        bandwidth = self.exp_config.if_filter_bw
        low_cutoff = freq - bandwidth / 2
        high_cutoff = freq + bandwidth / 2

        filter = get_filter(bounds=[low_cutoff, high_cutoff],
                            samp_rate=self.exp_config.samp_rate,
                            btype='band', order=order)
        return filter

    def _load_save_filter(self, order: int = 4):
        # Anti-alias LPF ahead of the stage-2 decimation, designed at the
        # stage-1 rate with its cutoff at 0.4x the final save rate
        save_rate = self.exp_config.samp_rate / self.exp_config.save_ds
        filter = get_filter(bounds=[0.4 * save_rate],
                            samp_rate=self.exp_config.samp_rate / self.ds1,
                            btype='low', order=order)
        return filter

    def _load_baseband_filter(self, cutoff: float = 12e3, order: int = 4):
        # Real anti-alias LPF ahead of the stage-1 (calibration-rate) decimation - a plain block
        # average (the old approach) is not steep enough to protect the
        # asymmetric-triangle calibration probe (2 kHz fundamental + harmonics)
        # from aliasing. 12 kHz keeps the main harmonics and is far above real
        # motion content.
        filter = get_filter(bounds=[cutoff],
                            samp_rate=self.exp_config.samp_rate,
                            btype='low', order=order)
        return filter

    def _process_chunk(self,
                       data,
                       filter,
                       if_freq,
                       channel_key,
                       t_idx
        ):
        # Early return for empty data
        empty = (np.array([]), np.array([]))
        if len(data) == 0:
            return empty, empty
        
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

        # Anti-alias LPF before decimation (a plain block average is not a
        # steep enough filter to protect the triangle probe's harmonics)
        current_bb_state = self.baseband_filter_states.get(channel_key)
        bb_filt, new_bb_state = apply_filter(baseband_data, self.baseband_filts[t_idx], zi=current_bb_state)
        self.baseband_filter_states[channel_key] = new_bb_state

        # Stage 1: calibration-rate stream
        windows = self._decimate(bb_filt, self.ds1, self.decim_remainder, channel_key)
        if windows is None:
            return empty, empty
        cal_comps = self._components(windows)

        # Stage 2: saved/displayed stream
        if self.ds2 == 1:
            return cal_comps, cal_comps

        # Filter the complex stage-1 signal (not amp/phase, whose phase wraps
        # would make the filter ring), then decimate again
        current_save_state = self.save_filter_states.get(channel_key)
        save_filt, new_save_state = apply_filter(windows.mean(axis=1), self.save_filt, zi=current_save_state)
        self.save_filter_states[channel_key] = new_save_state

        windows2 = self._decimate(save_filt, self.ds2, self.decim2_remainder, channel_key)
        if windows2 is None:
            return cal_comps, empty
        return cal_comps, self._components(windows2)

    def _decimate(self, data, step, remainders, channel_key):
        ''' Split data into windows of `step` samples, carrying any leftover
        (<step) samples from the previous chunk so the decimation phase stays
        continuous across chunk boundaries instead of silently dropping a tail
        every chunk. Returns (n, step) windows, or None if there's not one. '''
        stream = np.concatenate([remainders[channel_key], data])
        n_full = (len(stream) // step) * step
        remainders[channel_key] = stream[n_full:]

        if n_full == 0:
            return None
        return stream[:n_full].reshape(-1, step)

    def _components(self, windows):
        if self.save_iq:
            first_comp = np.mean(np.real(windows), axis=1)
            second_comp = np.mean(np.imag(windows), axis=1)
        else:
            first_comp = np.mean(np.abs(windows), axis=1)
            second_comp = np.mean(np.angle(windows, True), axis=1)

        return first_comp, second_comp
    
    def _process(self, buffer):
        # Output length per channel is no longer a fixed function of
        # buffer.shape[1] now that decimation carries a remainder across
        # chunks (and buffer width itself can vary, see ReceiveWorker) - so
        # collect per-channel results and stack afterward instead of writing
        # into a size-preallocated array.
        cal_results = {}
        save_results = {}

        for r_idx, row in enumerate(self.exp_config.channel_mapping):
            x = buffer[r_idx, :]
            for t_idx, channel_key in enumerate(row):
                channel_idx = self.exp_config.data_mapping[channel_key]
                # Pass the channel key for state tracking
                cal_comps, save_comps = self._process_chunk(
                    data = x,
                    filter = self.if_filts[t_idx],
                    if_freq = self.exp_config.channel_ifs[t_idx],
                    channel_key = channel_key,
                    t_idx = t_idx
                )
                self.logEvent.emit('debug', f'Processed channel {channel_key} with index {channel_idx}')
                cal_results[channel_idx] = np.stack(cal_comps, axis=-1)
                save_results[channel_idx] = np.stack(save_comps, axis=-1)

        # Return all processed samples, at the calibration and save rates
        num_channels = len(self.exp_config.data_mapping)
        cal_list = np.stack([cal_results[idx] for idx in range(num_channels)], axis=0)
        save_list = np.stack([save_results[idx] for idx in range(num_channels)], axis=0)
        return cal_list, save_list

    def _handle_batch(self, data_buf, dset):
        buffer_data = np.transpose(np.vstack(data_buf))
        cal_data, processed = self._process(buffer_data)
        start_sample = self.samples_processed
        self.samples_processed += buffer_data.shape[1]
        self.logEvent.emit('debug', f'Processed data shape {processed.shape}')

        # Add to display queue
        try:
            self.disp_queue.put_nowait(processed)
        except queue.Full:
            self.logEvent.emit('debug', 'Display Queue Full')

        # Let any other in-process consumer (e.g. calibration analysis) react
        # to each processed batch without going through a queue
        self.data_ready.emit({'data': cal_data, 'mapping': self.exp_config.data_mapping,
                              'start_sample': start_sample, 'end_sample': self.samples_processed})

        # Write to file, only if saving
        if dset is not None:
            append_save_chunk(dset, processed)

    def _check_backlog(self, item_width):
        # The Rx queues are unbounded: if processing can't keep up with the
        # radio, data piles up and everything downstream (file, plots,
        # calibration) runs late. Make that visible instead of silent.
        backlog = max(q.qsize() for q in self.rx_queues)
        backlog_s = backlog * item_width / self.exp_config.samp_rate
        if backlog_s > BACKLOG_WARN_S:
            self.logEvent.emit('warning', f'Save pipeline is {backlog_s:.1f} s behind real time ({backlog} buffers queued)')

    def _skip_samples(self, dev_idx, gap):
        ''' The receiver dropped `gap` samples on device dev_idx (overflow).
        Advance the downconversion phase of that device's channels by the
        same amount, so it stays locked to device time - otherwise the
        drop turns into a lasting phase step of 2*pi*f_IF*gap/fs. '''
        for r_idx in self.usrp_config[dev_idx].absolute_channel_nums:
            for t_idx, channel_key in enumerate(self.exp_config.channel_mapping[r_idx]):
                if channel_key in self.phase_accumulator:
                    advance = 2 * np.pi * self.exp_config.channel_ifs[t_idx] * gap / self.exp_config.samp_rate
                    self.phase_accumulator[channel_key] = (self.phase_accumulator[channel_key] + advance) % (2 * np.pi)

    def _add_items(self, items, data_buf, dset):
        ''' items: one (data, first_sample_n, gap) tuple per device queue. '''
        gaps = [gap for _, _, gap in items]
        if any(gaps):
            # Process everything before the gap with the old phase first
            if data_buf:
                self._handle_batch(data_buf, dset)
                data_buf = []
            for dev_idx, gap in enumerate(gaps):
                if gap:
                    self._skip_samples(dev_idx, gap)
        data_buf.append(np.transpose(np.vstack([data for data, _, _ in items])))
        return data_buf

    def run(self):
        self.logEvent.emit('debug', 'Saving started')

        # Keep the file open for the whole recording instead of reopening it for
        # every chunk
        h5 = h5py.File(self.out_file, 'a') if self.saving else None
        dset = h5['data'] if h5 is not None else None

        data_buf = []
        samples = [None] * len(self.rx_queues)
        last_backlog_check = time.monotonic()

        try:
            while self.running:
                try:
                    # Get from all queues
                    if len(data_buf) < self.buffer_size:
                        for idx, rx_q in enumerate(self.rx_queues):
                            samples[idx] = rx_q.get(timeout=0.5)
                        data_buf = self._add_items(samples, data_buf, dset)
                    else:
                        self._handle_batch(data_buf, dset)
                        data_buf = []

                    now = time.monotonic()
                    if now - last_backlog_check >= BACKLOG_CHECK_S and samples[0] is not None:
                        self._check_backlog(samples[0][0].shape[-1])
                        last_backlog_check = now
                except queue.Empty:
                    self.logEvent.emit('debug', 'Rx Queue Empty')
                    continue
                except Exception as e:
                    self.logEvent.emit('error', f'Saving error: {e}')
                    continue

            # Stopped - process whatever the receivers already queued, so the
            # tail of the recording isn't silently dropped. (The receivers are
            # stopped first, so this terminates.)
            # Only take a set when every device has one, so queues stay aligned.
            while all(not rx_q.empty() for rx_q in self.rx_queues):
                for idx, rx_q in enumerate(self.rx_queues):
                    samples[idx] = rx_q.get_nowait()
                data_buf = self._add_items(samples, data_buf, dset)
                if len(data_buf) >= self.buffer_size:
                    self._handle_batch(data_buf, dset)
                    data_buf = []
            if data_buf:
                self._handle_batch(data_buf, dset)
        except Exception as e:
            self.logEvent.emit('error', f'Saving error while flushing: {e}')
        finally:
            if h5 is not None:
                h5.close()

        self.logEvent.emit('debug', 'Saving stopped')
        
    def stop(self):
        self.running = False