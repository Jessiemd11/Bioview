import queue
import numpy as np

from PyQt6.QtCore import QThread, pyqtSignal

from bioview.utils import get_filter, apply_filter
from bioview.types import ExperimentConfiguration

class DisplayWorker(QThread):
    dataReady = pyqtSignal(np.ndarray)
    logEvent = pyqtSignal(str, str)

    def __init__(self, 
                 config: ExperimentConfiguration, 
                 disp_queue: queue.Queue, 
                 running: bool = True,
                 parent = None
        ):
        super().__init__(parent)
        self.config = config 
        self.disp_filter = get_filter(
            bounds = config.disp_filter_spec['bounds'], 
            samp_rate = config.samp_rate // (config.disp_ds * config.save_ds), 
            btype = config.disp_filter_spec['btype'],
            ftype = config.disp_filter_spec['ftype']
        )
        self.disp_queue = disp_queue
        self.running = running

    def _process(self, data):
        disp_data = [None] * len(self.config.disp_channels) 
        for idx, channel_key in enumerate(self.config.disp_channels):
            channel_idx = self.config.data_mapping[channel_key]
            
            # Channel data is a 2D array of shape (num_samples, 2) representing real and imaginary components
            if self.config.show_phase: 
                channel_data = data[channel_idx, :, 1] 
            else: 
                channel_data = data[channel_idx, :, 0]
             
            # Downsample 
            channel_data = channel_data[::self.config.disp_ds]
               
            # Filter                                  
            disp_data[idx], _ = apply_filter(channel_data, self.disp_filter)
                
        # Return all processed data 
        return np.array(disp_data)
    
    def run(self):
        self.logEvent.emit('debug', 'Display started')
        
        while self.running:
            try:
                # Get samples from queue
                samples = self.disp_queue.get()                
                # Process
                processed = self._process(samples)                 
                self.dataReady.emit(processed)
            except queue.Empty:
                self.logEvent.emit('error', 'Queue Empty')
                continue
            except Exception as e:
                self.logEvent.emit('error', f'Display error: {e}')
                continue
                
        self.logEvent.emit('debug', 'Display stopped')
        
    def stop(self):
        self.running = False