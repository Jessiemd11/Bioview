"""Event annotation panel with dispatch-lag-immune timestamps.

Key fix
-------
Timestamps are derived from ``QKeyEvent.timestamp()`` -- the timestamp the
operating system attached to the input event when the key was physically
pressed -- rather than from ``time.time()`` at the moment Qt happens to
dispatch the event to the handler.

If the Qt main thread stalls (blocking ``thread.wait()`` calls, USRP stream
callbacks, plot redraws), queued key events are all dispatched in the same
event-loop iteration. Wall-clock timestamps taken in the handler then collapse
to within a few milliseconds of each other. Hardware timestamps do not.
"""

from datetime import datetime
import os
import time

import qtawesome as qta
from PyQt6.QtWidgets import (
    QApplication,
    QGroupBox,
    QHBoxLayout,
    QLineEdit,
    QPlainTextEdit,
    QTextEdit,
    QToolButton,
)
from PyQt6.QtCore import Qt, pyqtSignal, QEvent

from bioview.utils import get_qcolor


class AnnotateEventPanel(QGroupBox):
    logEvent = pyqtSignal(str, str)

    # --- Presenter / remote-control bindings -------------------------------
    KEY_MAP = {
        Qt.Key.Key_PageUp:     '含藥水 (Hold Water)',
        Qt.Key.Key_PageDown:   '吞藥水 (Swallow Water)',
        Qt.Key.Key_VolumeUp:   '吞口水 (Swallow Saliva)',
        Qt.Key.Key_VolumeDown: '結束 (End)',
    }

    # Ignore a repeat of the SAME key within this window (contact bounce).
    # Different keys are never suppressed.
    REPEAT_GUARD_S = 0.15

    # Warn in the log panel when Qt dispatched an event this late.
    LAG_WARN_S = 0.05

    ENCODING = 'utf-8-sig'  # BOM so Excel opens the log without mojibake

    def __init__(self, config, parent=None):
        super().__init__('Mark Events', parent)

        self.config = config
        self.log_path = config.get_log_path()

        self.start_time = None          # time.monotonic() at T=0
        self._log_file = None           # kept open: open() can block for ms
        self._sidecar_file = None
        self._hw_offset = None          # hw clock -> monotonic clock offset
        self._last_press = {}           # key -> monotonic time of last accept

        layout = QHBoxLayout()
        self.annotation_box = QPlainTextEdit(self)
        self.annotation_box.setReadOnly(False)
        layout.addWidget(self.annotation_box)

        self.make_annotation_button = QToolButton()
        self.make_annotation_button.setText('Mark Event')
        self.make_annotation_button.setIcon(
            qta.icon('fa6s.pen-to-square', color=get_qcolor('orange'))
        )
        self.make_annotation_button.setToolButtonStyle(
            Qt.ToolButtonStyle.ToolButtonTextUnderIcon
        )
        self.make_annotation_button.setEnabled(True)
        # NOTE: clicked emits `checked: bool`. Connecting the slot directly
        # would pass False as `custom_text`. Wrap it.
        self.make_annotation_button.clicked.connect(
            lambda: self.record_annotation()
        )
        layout.addWidget(self.make_annotation_button)

        self.setLayout(layout)

        # Application-wide filter: presenter keys are captured regardless of
        # which widget currently holds focus. Previously QPlainTextEdit ate
        # PageUp/PageDown before MainWindow.keyPressEvent ever saw them.
        app = QApplication.instance()
        if app is not None:
            app.installEventFilter(self)

    # ------------------------------------------------------------------
    # Key capture
    # ------------------------------------------------------------------
    def eventFilter(self, obj, event):
        # Cheap type check first: this filter sees every event in the app.
        if event.type() == QEvent.Type.KeyPress:
            if self._handle_key_press(event):
                return True  # consumed
        return super().eventFilter(obj, event)

    def _handle_key_press(self, event):
        """Return True if the key was consumed as an event mark."""
        label = self.KEY_MAP.get(event.key())
        if label is None:
            return False

        # Let the user type / scroll normally inside text entry widgets.
        focus = QApplication.focusWidget()
        if isinstance(focus, (QPlainTextEdit, QLineEdit, QTextEdit)):
            return False

        # Held-down key -> OS auto-repeat. One press, one mark.
        if event.isAutoRepeat():
            return True

        press_time, lag = self._press_time(event)

        last = self._last_press.get(event.key())
        if last is not None and (press_time - last) < self.REPEAT_GUARD_S:
            return True  # contact bounce on the same button
        self._last_press[event.key()] = press_time

        self.record_annotation(label, press_time=press_time, lag=lag)
        return True

    def _press_time(self, event):
        """Recover the true press time on the monotonic clock.

        ``event.timestamp()`` is milliseconds on the platform's input clock
        (X server time on Linux, GetMessageTime on Windows). Its epoch is
        unrelated to ``time.monotonic()``, so we calibrate an offset.

        Dispatch lag is always >= 0, therefore the SMALLEST observed
        ``(receive_time - hw_time)`` across the session is the best estimate
        of the true offset. We keep updating it downwards.

        Returns (press_time_monotonic, dispatch_lag_seconds).
        """
        t_recv = time.monotonic()
        hw_ms = event.timestamp()

        if not hw_ms:  # synthetic / injected events report 0
            return t_recv, 0.0

        hw_s = hw_ms / 1000.0
        delta = t_recv - hw_s

        if self._hw_offset is None or delta < self._hw_offset:
            self._hw_offset = delta

        press_time = hw_s + self._hw_offset
        return press_time, max(0.0, t_recv - press_time)

    # ------------------------------------------------------------------
    # Theme handling (unchanged)
    # ------------------------------------------------------------------
    def _update_icons(self):
        self.make_annotation_button.setIcon(
            qta.icon('fa6s.pen-to-square', color=get_qcolor('orange'))
        )

    def event(self, event):
        if event.type() == QEvent.Type.ApplicationPaletteChange:
            self._update_icons()
        return super().event(event)

    # ------------------------------------------------------------------
    # Session lifecycle
    # ------------------------------------------------------------------
    def set_start_time(self):
        """Define T=0 and open the log files."""
        self.start_time = time.monotonic()
        self._hw_offset = None
        self._last_press.clear()
        self.refresh_path()

    def refresh_path(self):
        """Update the log path and (re)open the file handles."""
        self._close_files()
        self.log_path = self.config.get_log_path()

        try:
            self._log_file = open(
                self.log_path, 'a', encoding=self.ENCODING, newline=''
            )
            sidecar_path = os.path.splitext(self.log_path)[0] + '_marks.csv'
            new_sidecar = not os.path.exists(sidecar_path) \
                or os.path.getsize(sidecar_path) == 0
            self._sidecar_file = open(
                sidecar_path, 'a', encoding=self.ENCODING, newline=''
            )
            if new_sidecar:
                self._sidecar_file.write(
                    'elapsed_s,press_monotonic_s,dispatch_lag_ms,'
                    'wall_clock_iso,label\n'
                )
                self._sidecar_file.flush()
        except Exception as e:
            self.logEvent.emit('error', f'Could not open annotation log: {e}')
            return

        self.logEvent.emit(
            'info', f'Annotation path updated to: {self.log_path}'
        )

    def _close_files(self):
        for handle in (self._log_file, self._sidecar_file):
            if handle is not None:
                try:
                    handle.flush()
                    handle.close()
                except Exception:
                    pass
        self._log_file = None
        self._sidecar_file = None

    def shutdown(self):
        """Call from MainWindow.stop_recording() / closeEvent()."""
        app = QApplication.instance()
        if app is not None:
            app.removeEventFilter(self)
        self._close_files()

    # ------------------------------------------------------------------
    # Writing marks
    # ------------------------------------------------------------------
    def record_annotation(self, custom_text=None, press_time=None, lag=0.0):
        """Write one event mark.

        press_time -- monotonic time of the physical press. Omitted for
                      button clicks, where 'now' is correct by definition.
        lag        -- how late Qt dispatched the event, for diagnostics.
        """
        try:
            if press_time is None:
                press_time = time.monotonic()

            if self.start_time is None:
                elapsed = None
                time_str = '0.00s (Not Running)'
                self.logEvent.emit(
                    'warning',
                    'Event marked before recording started; time is not valid.'
                )
            else:
                elapsed = press_time - self.start_time
                time_str = f'{elapsed:.3f}s'

            annotation = custom_text if custom_text else \
                self.annotation_box.toPlainText()
            if not annotation.strip():
                return

            if self._log_file is None:
                self.refresh_path()
                if self._log_file is None:
                    return

            # Primary log: format unchanged, so existing parsers still work.
            self._log_file.write(f'{time_str} - {annotation}\n')
            self._log_file.flush()

            if self._sidecar_file is not None:
                self._sidecar_file.write(
                    f'{"" if elapsed is None else f"{elapsed:.6f}"},'
                    f'{press_time:.6f},{lag * 1000:.1f},'
                    f'{datetime.now().isoformat(timespec="milliseconds")},'
                    f'"{annotation}"\n'
                )
                self._sidecar_file.flush()

            if not custom_text:
                self.annotation_box.clear()

            if lag > self.LAG_WARN_S:
                self.logEvent.emit(
                    'warning',
                    f'[{time_str}] {annotation} — GUI thread stalled '
                    f'{lag * 1000:.0f} ms; timestamp corrected from the '
                    f'hardware clock.'
                )
            else:
                self.logEvent.emit(
                    'info', f'[{time_str}] Event marked: {annotation}'
                )

        except Exception as e:
            self.logEvent.emit('error', f'An error occurred: {e}')
