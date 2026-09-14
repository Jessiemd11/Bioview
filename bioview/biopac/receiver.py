from ctypes import byref, c_double

from PyQt6.QtCore import QThread, pyqtSignal

from bioview.types import BiopacConfiguration
from bioview.utils import wrap_result_code

class ReceiveWorker(QThread):
    logEvent = pyqtSignal(str, str)

    def __init__(self, 
                 biopac,
                 config: BiopacConfiguration,  
                 bio_queue, 
                 running:bool = True,
                 parent = None
        ):
        super().__init__(parent)
        self.config = config 
        self.biopac = biopac 
        self.bio_queue = bio_queue
        self.running = running
    
    def run(self): 
        try: 
            # Start acquisition
            wrap_result_code(self.biopac.startAcquisition())
            num_channels = len(self.config.channels)
            data_buffer = (c_double * (num_channels + 1))()  # +1 for timestamp
            
            while self.running:                     
                # Get recent sample and add to queue
                if wrap_result_code(self.mpdev.getMostRecentSample(byref(data_buffer))): 
                    sample = [data_buffer[i] for i in range(num_channels + 1)]
                    self.bio_queue.put(sample)
                
        except Exception as e: 
            self.logEvent.emit('error', e)
        
    def stop(self):
        wrap_result_code(self.mpdev.stopAcquisition())
        self.running = False 
        