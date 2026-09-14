import qtawesome as qta
from PyQt6.QtWidgets import QGroupBox, QPushButton, QHBoxLayout, QLabel, QCheckBox
from PyQt6.QtCore import pyqtSignal, QEvent

from bioview.types import ConnectionStatus, RunningStatus
from bioview.utils import get_qcolor

class AppControlPanel(QGroupBox):
    # Define signals to emit changes to connection status
    # This is left verbose for cleaner debugging
    connectionInitiated = pyqtSignal()
    startRequested = pyqtSignal()
    stopRequested = pyqtSignal() 
    saveRequested = pyqtSignal(bool)
    instructionsEnabled = pyqtSignal(bool)
    balanceRequested = pyqtSignal()
    
    def __init__(self, parent=None):
        super().__init__('Control', parent)
        self.main_window = parent
        
        # Initialize UI
        layout = QHBoxLayout()
        padlen = 8
                         
        # Connect Button
        self.connect_button = QPushButton('   Connect')
        self.connect_button.setIcon(qta.icon('fa6s.house', color=get_qcolor('purple')))
        self.connect_button.setStyleSheet(f'padding: {padlen}px;')
        self.connect_button.clicked.connect(self.on_connect_clicked)
        layout.addWidget(self.connect_button)
        
        # Start Button
        self.start_button = QPushButton('   Start')
        self.start_button.setIcon(qta.icon('fa6s.play', color=get_qcolor('green')))
        self.start_button.setStyleSheet(f'padding: {padlen}px;')
        self.start_button.setEnabled(False)
        self.start_button.clicked.connect(self.on_start_clicked)
        layout.addWidget(self.start_button)
        
        # Saving Checkbox 
        self.save_checkbox = QCheckBox(' Save ?')
        self.save_checkbox.clicked.connect(self.on_save_toggled)
        layout.addWidget(self.save_checkbox)
        
        # Instructions Checkbox (for audio/popups, etc)
        self.instructions_checkbox = QCheckBox(' Instructions ?')
        self.instructions_checkbox.clicked.connect(self.on_instructions_toggled)
        layout.addWidget(self.instructions_checkbox)
        
        # Stop Button
        self.stop_button = QPushButton('   Stop')
        self.stop_button.setIcon(qta.icon('fa6s.stop', color=get_qcolor('red')))
        self.stop_button.setStyleSheet(f'padding: {padlen}px;')
        self.stop_button.setEnabled(False)
        self.stop_button.clicked.connect(self.on_stop_clicked)
        layout.addWidget(self.stop_button)
        
        # Balance Signal Checkbox
        self.balance_signal_button = QPushButton('   Balance Signal')
        self.balance_signal_button.setIcon(qta.icon('fa6s.rotate', color=get_qcolor('blue')))
        self.balance_signal_button.setStyleSheet(f'padding: {padlen}px;')
        self.balance_signal_button.setEnabled(False)
        self.balance_signal_button.clicked.connect(self.on_balance_clicked)
        layout.addWidget(self.balance_signal_button)
        
        # Routine selection - This will be experiment specific now
        
        layout.addStretch()
        
        self.setLayout(layout)
    
    # Handle theme changes 
    def _update_icons(self): 
        self.connect_button.setIcon(qta.icon('fa6s.house', color=get_qcolor('purple')))
        self.start_button.setIcon(qta.icon('fa6s.play', color=get_qcolor('green')))
        self.stop_button.setIcon(qta.icon('fa6s.stop', color=get_qcolor('red')))
        self.balance_signal_button.setIcon(qta.icon('fa6s.rotate', color=get_qcolor('blue')))
        
    def event(self, event): 
        if event.type() == QEvent.Type.ApplicationPaletteChange: 
            self._update_icons()
        return super().event(event)
        
    def update_button_states(self, connection_status, running_status):
        if connection_status == ConnectionStatus.CONNECTED: 
            self.start_button.setEnabled(running_status == RunningStatus.STOPPED)
            self.stop_button.setEnabled(running_status == RunningStatus.RUNNING)
            self.save_checkbox.setEnabled(running_status == RunningStatus.STOPPED)
            self.connect_button.setEnabled(False)
            self.balance_signal_button.setEnabled(True) 
        elif connection_status == ConnectionStatus.DISCONNECTED: 
            self.connect_button.setEnabled(True)
            self.start_button.setEnabled(False)
            self.save_checkbox.setEnabled(True)
            self.stop_button.setEnabled(False)
            self.balance_signal_button.setEnabled(False)
        elif connection_status == ConnectionStatus.CONNECTING: 
            self.connect_button.setEnabled(False)
            self.start_button.setEnabled(False)
            self.stop_button.setEnabled(False)
    
    def on_connect_clicked(self):
        self.connectionInitiated.emit()
    
    def on_start_clicked(self):
        self.startRequested.emit()
    
    def on_stop_clicked(self):
        self.stopRequested.emit()
    
    def on_balance_clicked(self):
        self.balanceRequested.emit()
        
    def on_save_toggled(self): 
        if self.save_checkbox.isChecked(): 
            self.saveRequested.emit(True)
        else:
            self.saveRequested.emit(False)
    
    def on_instructions_toggled(self): 
        if self.instructions_checkbox.isChecked(): 
            self.instructionsEnabled.emit(True)
        else:
            self.instructionsEnabled.emit(False)