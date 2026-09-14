from PyQt6.QtCore import QMutex

from bioview.utils import get_unique_path

''' 
We make some general assumptions, specifically - 
* Each device has two working channels 
* Each device uses the default data formats 
* Each device uses internal timing reference and clock 
* Each device sends waveforms of amplitude 1
'''
BASE_USRP_CONFIG = {
    'tx_amplitude': [1, 1], 
    'rx_channels': [0, 1], 
    'tx_channels': [0, 1], 
    'rx_subdev': 'A:A A:B', 
    'tx_subdev': 'A:A A:B', 
    'cpu_format': 'fc32', 
    'wire_format': 'sc16',
    'clock': 'internal',
    'pps': 'internal',
    'if_filter_bw': 5e3,
}

class UsrpConfiguration(): 
    def __init__(self,
                 device_name: str, 
                 if_freq: list, 
                 if_bandwidth: int, 
                 rx_gain: list,
                 tx_gain: list, 
                 samp_rate: int, 
                 carrier_freq: int,
                 **kwargs
    ):
        # Add inputs
        self.device_name = device_name
        self.if_freq = if_freq
        self.if_bandwidth = if_bandwidth
        self.rx_gain = rx_gain
        self.tx_gain = tx_gain
        self.samp_rate = samp_rate
        self.carrier_freq = carrier_freq
        
        # Add base config 
        self.tx_amplitude = kwargs.get('tx_amplitude', BASE_USRP_CONFIG['tx_amplitude'])
        self.rx_channels = kwargs.get('rx_channels', BASE_USRP_CONFIG['rx_channels'])
        self.tx_channels = kwargs.get('tx_channels', BASE_USRP_CONFIG['tx_channels'])
        self.rx_subdev = kwargs.get('rx_subdev', BASE_USRP_CONFIG['rx_subdev'])
        self.tx_subdev = kwargs.get('tx_subdev', BASE_USRP_CONFIG['tx_subdev'])
        self.cpu_format = kwargs.get('cpu_format', BASE_USRP_CONFIG['cpu_format'])
        self.wire_format = kwargs.get('wire_format', BASE_USRP_CONFIG['wire_format'])
        self.clock = kwargs.get('clock', BASE_USRP_CONFIG['clock'])
        self.pps = kwargs.get('pps', BASE_USRP_CONFIG['pps'])
        
        # Set-up default absolute channel mapping, assuming single device.
        # This assumes that Tx/Rx are always used in pairs 
        # This must be updated if using MIMO with multiple USRPs
        self.absolute_channel_nums = self.tx_channels 
        
        # Add mutex to make updates thread safe
        self.mutex = QMutex()
            
    # Getters and setters to ensure continuous updates 
    def get_param_value(self, param): 
        self.mutex.lock()
        value = getattr(self, param)
        self.mutex.unlock()
        return value
    
    def set_param_value(self, param, value):
        self.mutex.lock()
        # Ensure we match types 
        current_type = type(getattr(self, param))
        setattr(self, param, current_type(value))
        self.mutex.unlock()
    
class BiopacConfiguration(): 
    def __init__(self, 
                 device_name: str, 
                 channels: list, 
                 samp_rate: int = 1000, 
                 device_type: str = 'MP36',          
                 mpdev_path: str = None
    ):
        self.device_name = device_name
        self.channels = channels
        
        self.samp_rate = samp_rate
        
        # Essential for long-term generalizability of codebase
        self.device_type = device_type
        # By default, we load from $PATH and $ROOT but a custom path may be provided  
        self.mpdev_path = mpdev_path
    
    def get_samp_time(self):
        return 1000.0 / self.samp_rate
    
    def get_channels(self): 
        # Since the API expects 16 channels, ensure we always pad to return in the appropriate format
        return self.channels + [0] * (16 - len(self.channels))
    
# A collection of (mostly) device-agnostic configuration parameters
class ExperimentConfiguration():
    def __init__(self, 
                 save_dir: str, 
                 file_name: str, 
                 save_ds: int,
                 disp_ds: int,
                 disp_filter_spec: dict,
                 disp_channels: list = None,
                 save_phase: bool = True,
                 show_phase: bool = False,
                 loop_instructions: bool = True,
                 instruction_type: str = None, 
                 instruction_file: list[str] = [], 
                 instruction_interval: int = 5000, 
                 **kwargs
        ):
        self.save_dir = save_dir
        self.file_name = file_name
        self.save_ds = save_ds
        self.disp_ds = disp_ds
        self.disp_filter_spec = disp_filter_spec        
        self.disp_channels = disp_channels
        
        # USRP-Specific Configuration Variables
        self.if_filter_bw = kwargs.get('if_filter_bw', BASE_USRP_CONFIG['if_filter_bw'])
        self.save_phase = save_phase
        self.show_phase = show_phase
        
        # Declare mappings 
        self.channel_mapping = None 
        self.channel_ifs = {}
        self.data_mapping = {}
        self.display_sources = [] # Collection of all things to display 

        ### Common functionality for instructions 
        self.loop_instructions = loop_instructions
        self.instruction_type = instruction_type # Typically audio or text
        self.instruction_file = instruction_file
        self.instruction_interval = instruction_interval 
        
        # Add mutex to make updates thread safe
        self.mutex = QMutex()
        
    def set_param_value(self, param, value):
        self.mutex.lock()
        # Ensure we match types 
        current_type = type(getattr(self, param, None))
        if current_type is not None:
            setattr(self, param, current_type(value))
        else:
            setattr(self, param, value)
        self.mutex.unlock()
    
    def get_param(self, param, default_value = None):
        self.mutex.lock()
        try:
            value = getattr(self, param)  
        except:
            value = default_value 
        self.mutex.unlock()
        return value
        
    def get_log_path(self): 
        return get_unique_path(self.save_dir, f'{self.file_name}.log')
        
    def get_save_path(self):
        return get_unique_path(self.save_dir, f'{self.file_name}.h5') 