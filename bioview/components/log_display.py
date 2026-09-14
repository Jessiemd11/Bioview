import logging 
from PyQt6.QtWidgets import QGroupBox, QVBoxLayout, QPlainTextEdit
from PyQt6.QtCore import QObject, pyqtSignal
    
# Make a thread safe logging handler 
class QTextEditLogger(QObject, logging.Handler):
    update_log = pyqtSignal(str)

    def __init__(self, text_box):
        super().__init__()
        self.text_box = text_box
        self.update_log.connect(self.append_text)
        self.flushOnClose = False 

    def emit(self, record):
        msg = self.format(record)
        self.update_log.emit(msg)
        
    def append_text(self, text):
        cursor = self.text_box.textCursor()
        cursor.movePosition(cursor.MoveOperation.End)
        cursor.insertText(text + '\n')
        self.text_box.setTextCursor(cursor)
        # Auto-scroll to the bottom
        self.text_box.ensureCursorVisible()
    
class LogDisplayPanel(QGroupBox):
    def __init__(self, 
                 logger, 
                 parent=None
        ):
        super().__init__('Log', parent)    
        layout = QVBoxLayout()
        self.log_text_box = QPlainTextEdit(self)
        self.log_text_box.setReadOnly(True)

        layout.addWidget(self.log_text_box)
        self.setLayout(layout)
        
        # Create and add the custom logger handler
        self.logger = logger
        self.log_handler = QTextEditLogger(self.log_text_box)
        formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s',
                                      datefmt='%H:%M:%S')
        self.log_handler.setFormatter(formatter)
        self.logger.addHandler(self.log_handler)
        
    def log_message(self, level, msg):
        log_method = getattr(self.logger, level, None)
        if log_method is not None:
            log_method(msg)