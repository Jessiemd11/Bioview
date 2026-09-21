import numpy as np
import pyqtgraph as pg

from PyQt6.QtWidgets import QDialog, QGridLayout
from PyQt6.QtCore import Qt

from bioview.utils import get_color_by_idx

# Default half-height of the plot. Traces that exceed it are NOT clipped: the
# axis grows to fit and the title says so, so a badly scaled probe shows up as
# an obviously blown-out curve instead of an empty-looking plot.
DEFAULT_Y_LIM = 1.5

class CalibrationProbeDialog(QDialog):
    '''
    Side-by-side static view of the start-of-recording and end-of-recording
    calibration bursts, lag-aligned and normalized like the paper's Fig. 2/4 -
    a quick visual sanity check that the injected triangle probe actually
    showed up at Rx, and whether the channel drifted over the recording.

    One row per channel label, two columns (start | end). Each cell shows the
    best burst (highest |NCC|) for that channel. Failures are made visible:
    a trace that exceeds the default range rescales the axis and says so in
    the title, and a channel with no usable burst says so in the plot.
    '''
    def __init__(self, start_view: dict, end_view: dict, parent=None):
        super().__init__(parent)
        self.setWindowTitle('Calibration Probe (aligned, normalized) - start vs end')
        self.setWindowFlags(Qt.WindowType.Window | Qt.WindowType.WindowStaysOnTopHint)
        self.resize(1000, 700)

        layout = QGridLayout()
        labels = sorted(set(start_view) | set(end_view))

        for row, label in enumerate(labels):
            for col, (phase, view_dict) in enumerate((('start', start_view), ('end', end_view))):
                view = view_dict.get(label)

                widget = pg.PlotWidget()
                widget.setBackground(None)
                widget.showGrid(x=True, y=True)
                widget.setLabel('bottom', 'Time (ms)')
                widget.setLabel('left', 'Normalized amplitude')

                title = f'{label} ({phase})'
                y_lim = DEFAULT_Y_LIM

                if view is not None:
                    # Direct point-to-point lines only - no spline smoothing, which
                    # would manufacture fake overshoot at this sample rate (~25
                    # points/cycle).
                    tx_pen = pg.mkPen(color=(150, 150, 150), width=1, style=Qt.PenStyle.DotLine)
                    rx_pen = pg.mkPen(color=get_color_by_idx(row), width=1.5)
                    widget.plot(view['t_ms'], view['tx_norm'], pen=tx_pen, name='Tx reference')
                    widget.plot(view['t_ms'], view['rx_norm'], pen=rx_pen, name='Rx (aligned)')

                    title += f'  |ncc|={abs(view["ncc"]):.2f}'
                    peak = float(max(np.max(np.abs(view['rx_norm'])), np.max(np.abs(view['tx_norm']))))
                    if not np.isfinite(peak):
                        title += '  [non-finite values in trace]'
                    elif peak > DEFAULT_Y_LIM:
                        y_lim = 1.05 * peak
                        title += f'  [rescaled: peak {peak:.1f}]'
                else:
                    title += '  [no valid burst]'
                    text = pg.TextItem('no valid burst', anchor=(0.5, 0.5))
                    text.setPos(0.0, 0.0)
                    widget.addItem(text)

                widget.setTitle(title)
                widget.setYRange(-y_lim, y_lim)

                layout.addWidget(widget, row, col)

        self.setLayout(layout)
