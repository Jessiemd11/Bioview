from PyQt6.QtWidgets import QGridLayout, QLabel, QGroupBox, QDoubleSpinBox
from PyQt6.QtCore import pyqtSignal

from bioview.types import UsrpConfiguration

class UsrpDeviceConfigPanel(QGroupBox):
    modifyUSRP = pyqtSignal(str, object)
    logEvent = pyqtSignal(str, str)
    
    def __init__(self, 
                 config: UsrpConfiguration,  
                 parent = None
        ):
        super().__init__(f'{config.get_param_value('device_name')} Settings', parent)
        self.config = config 
        self.param_inputs = {}
        self.init_ui()
    
    def init_ui(self):
        layout = QGridLayout()
        
        # Create parameter inputs
        param_mappings = {
            'tx_gain': {
                'disp_str': 'TX Gain (dB)', 
                'range': (0, 70), 
                'multiplier': 1, # In case units need a multiplier during storage, such as for frequency 
                'step': 1
            },
            'rx_gain': {
                'disp_str': 'RX Gain (dB)', 
                'range': (0, 70), 
                'multiplier': 1,
                'step': 0.1
            },
            'tx_amplitude': {
                'disp_str': 'IF Amplitude', 
                'range': (0, 1), 
                'multiplier': 1,
                'step': 0.01
            },
            'if_freq': {
                'disp_str': 'IF Frequency (kHz)', 
                'range': (20, 400), 
                'multiplier': 1e3, # kHz to Hz 
                'step': 0.1
            },
            'samp_rate': {
                'disp_str': 'Sample Rate (MSps)', 
                'range': (0.1, 10), 
                'multiplier': 1e6, # MSps to sps
                'step': 0.05
            },
            'carrier_freq': {
                'disp_str': 'Carrier Freq. (MHz)', 
                'range': (30, 6000), 
                'multiplier': 1e6, # MHz to Hz
                'step': 1
            },
        }
        
        row = 0
        for param_name, val in param_mappings.items(): 
            label_text = val['disp_str']
            min_val = val['range'][0]
            max_val = val['range'][1]
            multiplier = val['multiplier']
            step = val['step']
            
            # Add text for widget 
            layout.addWidget(QLabel(label_text), row, 0)
            
            # Make as many spin boxes as channels specified  
            current_values = self.config.get_param_value(param_name)
            values = current_values if isinstance(current_values, (list, tuple)) else [current_values]
            
            input_widgets = []
                
            for col, value in enumerate(values): 
                # Make current value an integer
                display_value = value / multiplier if isinstance(value, (int, float)) else value
                
                # Make widget 
                widget = QDoubleSpinBox()
                widget.setRange(min_val, max_val)
                widget.setDecimals(2)
                widget.setSingleStep(step)
                widget.setValue(display_value)
            
                # Connect signal 
                idx = col if len(values) > 1 else None
                val = value * multiplier
                widget.valueChanged.connect(
                    lambda val, param_name=param_name, idx=idx: self.modify_usrp_config(
                        param_name=param_name, 
                        value=val,
                        idx=idx
                    )
                )

                layout.addWidget(widget, row, col+1)
                input_widgets.append(widget)
            
            self.param_inputs[param_name] = input_widgets
            row += 1
        
        self.setLayout(layout)
    
    # TODO: Change this to connect params
    def modify_usrp_config(self, param_name, value, idx=None):
        # We may have lists here, these need to be handled correctly
        if idx is not None: 
            # Get value, update idx from list and emit overall
            updated_value = self.config.get_param_value(param_name)
            updated_value[idx] = value
        else:
            updated_value = value
        
        try: 
            self.config.set_param_value(param_name, updated_value)
            self.logEvent.emit('debug', f'{self.config.device_name}: Updated {param_name} to {value} successfully')
        except Exception as e:
            self.logEvent.emit('error', f'{self.config.device_name}: Updating {param_name} failed')