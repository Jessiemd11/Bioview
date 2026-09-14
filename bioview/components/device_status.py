from PyQt6.QtWidgets import QWidget, QHBoxLayout, QLabel
from PyQt6.QtCore import QTimer, QEvent
from PyQt6.QtGui import QPainter, QColor, QPen

from bioview.types import ConnectionStatus

class LEDIndicator(QWidget):
    ''' Indicate device status using the following codes - 
    Connected: Solid Green , 
    Connecting: Blinking Yellow, 
    Disconnected: Solid Red
    '''
    def __init__(self, 
                 state=ConnectionStatus.DISCONNECTED, 
                 size: int = 12
    ):
        super().__init__()
        self.state = state
        self.size = size
        self.blink_visible = False
        self.setFixedSize(size, size)
        
        # Timer for blinking effect
        self.blink_timer = QTimer()
        self.blink_timer.timeout.connect(self.toggle_blink)
        
        self.update_state(state)
    
    def update_state(self, state):
        self.state = state
        
        if state == ConnectionStatus.CONNECTING:
            self.blink_timer.start(100) 
        else:
            self.blink_timer.stop()
            self.blink_visible = True
        
        self.update()
    
    def toggle_blink(self):
        self.blink_visible = not self.blink_visible
        self.update()
    
    def paintEvent(self, event):
        # Draw the LED circle with appropriate color
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    
        if self.state == ConnectionStatus.CONNECTING and not self.blink_visible: 
            color = QColor(100, 100, 0) 
        else: 
            color = self.state.value[1]
        
        painter.setBrush(color)
        painter.setPen(QPen(QColor(50, 50, 50), 1))
        
        margin = 1
        painter.drawEllipse(margin, margin, self.size - 2*margin, self.size - 2*margin)
        
class DeviceStatusWidget(QWidget):
    def __init__(self, 
                 device_name, 
                 device_state = ConnectionStatus.DISCONNECTED
    ):
        super().__init__()
        self.device_name = device_name
        self.device_state = device_state
        
        # Create horizontal layout
        layout = QHBoxLayout()
        layout.setContentsMargins(5, 2, 5, 2)
        layout.setSpacing(5)
        
        self.label = QLabel(device_name)
        self.indicator = LEDIndicator(device_state)
        
        # Add widgets to layout
        layout.addWidget(self.label)
        layout.addWidget(self.indicator)
        
        self.setLayout(layout)
    
    def update_state(self, new_state):
        self.device_state = new_state
        self.indicator.update_state(new_state)
        
class DeviceStatusPanel(QWidget):
    def __init__(self, devices: dict = {}):
        super().__init__()
        self.devices = devices
        self.device_widgets = {}
        
        # Create horizontal layout for all devices
        self.layout = QHBoxLayout()
        self.layout.setContentsMargins(0, 0, 0, 0)
        self.layout.setSpacing(15)
        
        # Add device widgets
        for device_name, device_state in self.devices.items():
            self.add_device(device_name, device_state)
        
        self.setLayout(self.layout)
    
    # Handle theme changes 
    def _update_icons(self): 
        for device_name, device_state in self.devices.items():
            self.device_widgets[device_name].update_state(device_state)
        
    def event(self, event): 
        if event.type() == QEvent.Type.ApplicationPaletteChange: 
            self._update_icons()
        return super().event(event)
    
    def add_device(self, 
                   device_name, 
                   device_state=ConnectionStatus.DISCONNECTED
    ):
        device_widget = DeviceStatusWidget(device_name, device_state)
        self.device_widgets[device_name] = device_widget
        self.layout.addWidget(device_widget)
        self.devices[device_name] = device_state
    
    def update_device_state(self, device_name, new_state):
        if device_name in self.device_widgets:
            self.device_widgets[device_name].update_state(new_state)
            self.devices[device_name] = new_state
    
    def remove_device(self, device_name):
        if device_name in self.device_widgets:
            widget = self.device_widgets[device_name]
            self.layout.removeWidget(widget)
            widget.deleteLater()
            del self.device_widgets[device_name]
            del self.devices[device_name]