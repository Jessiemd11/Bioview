from ctypes import c_int, c_double, byref

from PyQt6.QtCore import QThread, pyqtSignal

from bioview.utils import load_mpdev_dll, wrap_result_code
from bioview.constants import BIOPAC_CONNECTION_CODES

class Controller(QThread):
    initSucceeded = pyqtSignal(object)
    initFailed = pyqtSignal(str)
    logEvent = pyqtSignal(str, str)

    def __init__(self, config, parent=None):
        super().__init__(parent)  
        self.config = config
        self.biopac = None 
    
    def run(self): 
        # Load the DLL
        self.biopac = load_mpdev_dll(self.config.mpdev_path)
        if self.mpdev is None: 
            self.logEvent.emit('mpdev.dll was not found. Ensure it is available in either the system path or the custom path provided')
            return 
        
        # Connect to the device - Currently, the types values are hard-coded but they needed to be taken from the device name 
        try:
            wrap_result_code(self.biopac.connectMPDev(c_int(103), c_int(10), b'auto'), 'Initialization')
            
            # Set channels
            if sum(self.config.channels) == 0:
                raise ValueError("At least one channel must be active")
            channels_array = (c_int * 16)(*self.config.get_channels())
            wrap_result_code(self.biopac.setAcqChannels(byref(channels_array)), 'Set Channels')
        
            # Set sample rate 
            wrap_result_code(self.mpdev.setSampleRate(c_double(self.config.get_sample_time())), 'Set Sample Rate')
            
            # Emit device to main app
            self.initSucceeded.emit(self.biopac)
        except Exception as e:
            self.initFailed.emit(f'Unable to initialize device: {e}')
            
    def close(self):
        # Cleanup device handler 
        try:
            wrap_result_code(self.biopac.disconnectMPDev())
        except Exception as e: 
            self.logEvent.emit('info', 'BIOPAC connection already closed')