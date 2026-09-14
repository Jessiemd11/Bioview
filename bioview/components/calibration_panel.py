import qtawesome as qta
from PyQt6.QtWidgets import QGroupBox, QPushButton, QHBoxLayout, QVBoxLayout, QLabel
from PyQt6.QtCore import pyqtSignal, QEvent, Qt

from bioview.types import ConnectionStatus, RunningStatus, ChannelQualityStatus
from bioview.utils import get_qcolor
from bioview.components.device_status import LEDIndicator

class CalibrationPanel(QGroupBox):
    calibrationRequested = pyqtSignal()

    def __init__(self, channel_labels: list, parent=None):
        super().__init__('Calibration', parent)
        self.channel_labels = list(channel_labels)
        self.calibrating = False

        layout = QVBoxLayout()

        top_row = QHBoxLayout()
        self.calibrate_button = QPushButton('   Calibrate')
        self.calibrate_button.setIcon(qta.icon('fa6s.crosshairs', color=get_qcolor('teal')))
        self.calibrate_button.setStyleSheet('padding: 8px;')
        self.calibrate_button.setEnabled(False)
        self.calibrate_button.clicked.connect(self.on_calibrate_clicked)
        top_row.addWidget(self.calibrate_button)

        self.summary_label = QLabel('')
        top_row.addWidget(self.summary_label)
        top_row.addStretch()
        layout.addLayout(top_row)

        # Four (or however many) traffic-light rows, one per Tx/Rx channel pair
        lights_row = QHBoxLayout()
        self.indicators = {}
        for label in self.channel_labels:
            col = QVBoxLayout()
            indicator = LEDIndicator(ChannelQualityStatus.PENDING, size=16)
            self.indicators[label] = indicator
            col.addWidget(indicator, alignment=Qt.AlignmentFlag.AlignHCenter)
            col.addWidget(QLabel(label))
            lights_row.addLayout(col)
        layout.addLayout(lights_row)

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
            self.summary_label.setText('Calibrating...')
            for indicator in self.indicators.values():
                indicator.update_state(ChannelQualityStatus.PENDING)

    def show_result(self, channels: dict):
        parts = []
        for label, indicator in self.indicators.items():
            ch = channels.get(label)
            if ch is None:
                continue
            indicator.update_state(ch['quality'])
            parts.append(f"{label}: {ch['quality'].value[0]} (SNR {ch.get('snr_db', 0):.0f}dB)")
        self.summary_label.setText(' | '.join(parts))
