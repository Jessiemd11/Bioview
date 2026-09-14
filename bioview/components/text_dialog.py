from PyQt6.QtWidgets import QDialog, QVBoxLayout, QLabel
from PyQt6.QtCore import Qt, pyqtSlot

class TextDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle('Instructions')
        self.setGeometry(100, 100, 400, 300)
        self.setWindowFlags(Qt.WindowType.Window | Qt.WindowType.WindowStaysOnTopHint)
        
        # Setup UI
        layout = QVBoxLayout()
        
        self.text_display = QLabel()
        self.text_display.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.text_display.setWordWrap(True)
        self.text_display.setStyleSheet("""
            QLabel {
                padding: 20px;
                font-size: 16px;
                border-radius: 5px;
                border: 1px solid #ccc;
            }
        """)
        
        layout.addWidget(self.text_display)
        self.setLayout(layout)
        
        # Hide by default - main thread will show when needed
        self.hide()
    
    @pyqtSlot(str)
    def update_instruction_text(self, text):
        self.text_display.setText(text)
    
    @pyqtSlot(bool)
    def toggle_ui(self, enabled): 
        if enabled:
            self.show()
            self.raise_()  # Bring to front
            self.activateWindow()
        else:
            self.hide()