import qtawesome as qta
from PyQt6.QtWidgets import QGroupBox, QPushButton, QHBoxLayout, QLabel
from PyQt6.QtCore import pyqtSignal, QEvent

from bioview.types import ConnectionStatus, RunningStatus, ChannelQualityStatus
from bioview.utils import get_qcolor
from bioview.components.device_status import LEDIndicator

class CalibrationPanel(QGroupBox):
    '''
    Compact, single-row calibration control: one button plus one small
    LEDIndicator per Tx/Rx channel pair (hover an indicator for its quality/
    SNR detail). Sized to fit alongside the Log/Mark Event panels in the
    right-hand column rather than the taller two-row layout it started as.
    '''
    calibrationRequested = pyqtSignal()

    def __init__(self, channel_labels: list, parent=None):
        super().__init__('Calibration', parent)
        self.channel_labels = list(channel_labels)
        self.calibrating = False

        layout = QHBoxLayout()
        layout.setContentsMargins(6, 2, 6, 2)
        layout.setSpacing(6)

        self.calibrate_button = QPushButton(' Calibrate')
        self.calibrate_button.setIcon(qta.icon('fa6s.crosshairs', color=get_qcolor('teal')))
        self.calibrate_button.setEnabled(False)
        self.calibrate_button.clicked.connect(self.on_calibrate_clicked)
        layout.addWidget(self.calibrate_button)

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

    def _update_icons(self):
        self.calibrate_button.setIcon(qta.icon('fa6s.crosshairs', color=get_qcolor('teal')))

    def event(self, event):
        if event.type() == QEvent.Type.ApplicationPaletteChange:
            self._update_icons()
        return super().event(event)

    def on_calibrate_clicked(self):
        self.calibrationRequested.emit()

    def update_button_states(self, connection_status, running_status):
        can_calibrate = (connection_status == ConnectionStatus.CONNECTED
                          and running_status != RunningStatus.RUNNING
                          and not self.calibrating)
        self.calibrate_button.setEnabled(can_calibrate)

    def set_running(self, running: bool):
        self.calibrating = running
        self.calibrate_button.setEnabled(not running)
        if running:
            for label, indicator in self.indicators.items():
                indicator.update_state(ChannelQualityStatus.PENDING)
                indicator.setToolTip(f'{label}: calibrating...')

    def show_result(self, channels: dict):
        for label, indicator in self.indicators.items():
            ch = channels.get(label)
            if ch is None:
                continue
            indicator.update_state(ch['quality'])
            indicator.setToolTip(
                f"{label}: {ch['quality'].value[0]} "
                f"(amp {ch.get('amplitude', 0):.3g}, SNR {ch.get('snr_db', 0):.0f}dB)"
            )
