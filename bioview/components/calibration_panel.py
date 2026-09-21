from PyQt6.QtWidgets import QGroupBox, QCheckBox, QHBoxLayout, QLabel
from PyQt6.QtCore import pyqtSignal

from bioview.types import RunningStatus, ChannelQualityStatus
from bioview.components.device_status import LEDIndicator

class CalibrationPanel(QGroupBox):
    '''
    Compact, single-row calibration control: one "Calibrate ?" checkbox
    (same pattern as AppControlPanel's "Save ?") plus one small LEDIndicator
    per Tx/Rx channel pair (hover an indicator for its quality/SNR detail).
    When checked, a recording injects a short triangular-probe episode right
    after it starts and right before it stops (see Viewer.start_recording/
    stop_recording) instead of calibration being its own separate session.
    '''
    calibrationEnabled = pyqtSignal(bool)

    def __init__(self, channel_labels: list, parent=None):
        super().__init__('Calibration', parent)
        self.channel_labels = list(channel_labels)

        layout = QHBoxLayout()
        layout.setContentsMargins(6, 2, 6, 2)
        layout.setSpacing(6)

        self.calibrate_checkbox = QCheckBox(' Calibrate ?')
        self.calibrate_checkbox.clicked.connect(self.on_calibrate_toggled)
        layout.addWidget(self.calibrate_checkbox)

        self.indicators = {}
        for label in self.channel_labels:
            lbl = QLabel(label)
            indicator = LEDIndicator(ChannelQualityStatus.PENDING, size=12)
            indicator.setToolTip(f'{label}: not yet calibrated')
            self.indicators[label] = indicator
            layout.addWidget(lbl)
            layout.addWidget(indicator)

        layout.addStretch()
        self.setLayout(layout)

    def on_calibrate_toggled(self):
        self.calibrationEnabled.emit(self.calibrate_checkbox.isChecked())

    def update_button_states(self, connection_status, running_status):
        self.calibrate_checkbox.setEnabled(running_status == RunningStatus.STOPPED)

    def set_running(self, running: bool):
        '''Called at the start of a calibration episode to reset the LEDs to
        PENDING while the probe is being injected/analyzed.'''
        if running:
            for label, indicator in self.indicators.items():
                indicator.update_state(ChannelQualityStatus.PENDING)
                indicator.setToolTip(f'{label}: calibrating...')

    def show_result(self, channels: dict):
        '''Called once per episode, after CalibrationAnalyzer.result() has
        run on the fully-resolved episode - not live/burst-by-burst.'''
        for label, indicator in self.indicators.items():
            ch = channels.get(label)
            if ch is None:
                continue
            indicator.update_state(ch['quality'])
            indicator.setToolTip(
                f"{label}: {ch['quality'].value[0]} ({ch.get('reason', '')}) - "
                f"gain {ch.get('mean_gain_db', float('nan')):.1f}dB, "
                f"SNR {ch.get('mean_snr_db', float('nan')):.1f}dB, "
                f"NCC {ch.get('mean_ncc', float('nan')):.2f}"
            )
