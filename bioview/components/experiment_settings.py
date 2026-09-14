import qtawesome as qta
from PyQt6.QtWidgets import (
    QGridLayout, QLabel, QGroupBox, QFileDialog, QSpinBox, QListView,
    QComboBox, QLineEdit, QPushButton, QHBoxLayout, QDoubleSpinBox
)
from PyQt6.QtCore import Qt, pyqtSignal, QEvent
from PyQt6.QtGui import QStandardItemModel, QStandardItem

from bioview.types import ConnectionStatus, RunningStatus, ExperimentConfiguration
from bioview.utils import get_qcolor

class CheckableListView(QListView):
    def __init__(self, combo_box):
        super().__init__(combo_box)
        self.combo_box = combo_box

    def mouseReleaseEvent(self, event):
        index = self.indexAt(event.pos())
        if index.isValid():
            item = self.model().itemFromIndex(index)
            self.combo_box.toggle_item(item) 
            self.viewport().update()
            self.combo_box.showPopup()
        else:
            super().mouseReleaseEvent(event)

class CheckableComboBox(QComboBox):
    selectionChanged = pyqtSignal(str, str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._view = CheckableListView(self)
        self.setView(self._view)
        self.setModel(QStandardItemModel(self))
        self.view().viewport().installEventFilter(self)
        self.setEditable(True)
        self.lineEdit().setReadOnly(True)
        self.lineEdit().setPlaceholderText('Select options...')
        self.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self.clearEditText()

    def addItem(self, text: str, checked=False):
        item = QStandardItem(text)
        item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsUserCheckable)
        item.setData(Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked,
                     Qt.ItemDataRole.CheckStateRole)
        self.model().appendRow(item)
        if checked:
            self.selectionChanged.emit('add', text) 

    def toggle_item(self, item: QStandardItem):
        if item.checkState() == Qt.CheckState.Checked:
            self.selectionChanged.emit('remove', item.text())
        else:
            item.setCheckState(Qt.CheckState.Checked)
            self.selectionChanged.emit('add', item.text())

    def select_channel(self, text: str):
        for i in range(self.model().rowCount()):
            item = self.model().item(i)
            if item.text() == text and item.checkState() != Qt.CheckState.Checked:
                item.setCheckState(Qt.CheckState.Checked)
                self.update_line_text()
                break

    def unselect_channel(self, text: str):
        for i in range(self.model().rowCount()):
            item = self.model().item(i)
            if item.text() == text and item.checkState() != Qt.CheckState.Unchecked:
                item.setCheckState(Qt.CheckState.Unchecked)
                self.update_line_text()
                break

    def checkedItems(self):
        return [
            self.model().item(i).text()
            for i in range(self.model().rowCount())
            if self.model().item(i).checkState() == Qt.CheckState.Checked
        ]

    def update_line_text(self):
        checked = self.checkedItems()
        self.lineEdit().setText(", ".join(checked) if checked else "")
        
    def eventFilter(self, source, event):
        if event.type() == QEvent.Type.MouseButtonPress:
            index = self.view().indexAt(event.pos())
            if not index.isValid():
                self.hidePopup()
        return super().eventFilter(source, event)
    
class ExperimentSettingsPanel(QGroupBox):    
    parameterChanged = pyqtSignal(str, object)
    logEvent = pyqtSignal(str, str)
    timeWindowChanged = pyqtSignal(int)
    gridLayoutChanged = pyqtSignal(int, int)
    addChannelRequested = pyqtSignal(str)
    removeChannelRequested = pyqtSignal(str)

    def __init__(self, 
                 config: ExperimentConfiguration,  
                 parent=None
        ):
        super().__init__('Experiment Settings', parent)
        self.config = config
        self.param_inputs = {}
        self.init_ui()
    
    def init_ui(self):
        layout = QGridLayout()
        row = 0 
        
        # Create parameter inputs
        file_name = getattr(self.config, 'file_name')
        save_dir = getattr(self.config, 'save_dir')
        
        # File Name 
        layout.addWidget(QLabel("File Name"), row, 0)
        self.file_name = QLineEdit()
        self.file_name.setText(file_name)
        self.file_name.setFocusPolicy(Qt.FocusPolicy.ClickFocus)
        self.file_name.textChanged.connect(
            lambda value: self.update_param(
                param_name='file_name', 
                value=value
            )
        )
        layout.addWidget(self.file_name, row, 1)
        row += 1        
        
        # Folder picker
        layout.addWidget(QLabel("Save Path"), row, 0)
        
        picker_layout = QHBoxLayout()
        self.folder_path = QLineEdit()
        self.folder_path.setReadOnly(True)  # Make it read-only
        self.folder_path.setText(save_dir)
        # Update folder_path as it changes 
        self.folder_path.textChanged.connect(
            lambda value: self.update_param(
                param_name='save_dir', 
                value=value
            )
        )
        picker_layout.addWidget(self.folder_path)
        
        # Button to trigger folder selection dialog
        self.browse_button = QPushButton('  Browse')
        self.browse_button.setIcon(qta.icon('fa6s.folder', color=get_qcolor('mint')))
        self.browse_button.clicked.connect(self.openFolderDialog)
        picker_layout.addWidget(self.browse_button)
        
        layout.addLayout(picker_layout, row, 1)
        row += 1        
        
        # Downsampling ratios 
        for key, val in {'save_ds': 'Saving', 'disp_ds': 'Display'}.items(): 
            layout.addWidget(QLabel(f'{val} Downsample'), row, 0)
            widget = QDoubleSpinBox()
            widget.setRange(1, 1000)
            widget.setDecimals(0)
            widget.setSingleStep(10)
            widget.setValue(getattr(self.config, key))
            # Connect to parameter changer
            widget.textChanged.connect(
                lambda value, key=key: self.update_param(
                    param_name=key, 
                    value=value
                )
            )
            layout.addWidget(widget, row, 1)
            row += 1
        
        # Display Time Length 
        layout.addWidget(QLabel("Display Time (s)"), row, 0)
        time_layout = QHBoxLayout()
        self.time_input = QSpinBox()
        self.time_input.setRange(1, 30)
        self.time_input.setValue(10) # Set initial value
        self.time_input.valueChanged.connect(self.update_display_time)
        time_layout.addWidget(self.time_input)
        layout.addLayout(time_layout, row, 1)
        row += 1
        
        # Grid Design 
        layout.addWidget(QLabel("Plot Layout"), row, 0)
        
        grid_layout = QHBoxLayout() 
        self.rows_input = QSpinBox()
        self.rows_input.setRange(1, 4)
        self.rows_input.setValue(2)
        self.rows_input.valueChanged.connect(self.update_grid)
        self.rows_input.setEnabled(True)
        grid_layout.addWidget(self.rows_input)
        
        self.cols_input = QSpinBox()
        self.cols_input.setRange(1, 3)
        self.cols_input.setValue(2)
        self.cols_input.valueChanged.connect(self.update_grid)
        self.rows_input.setEnabled(True)
        grid_layout.addWidget(self.cols_input)
        
        layout.addLayout(grid_layout, row, 1)
        row += 1
        
        # Channel selection
        layout.addWidget(QLabel('Plot Sources'), row, 0)
        self.channel_combo = CheckableComboBox()
        for x in self.config.data_mapping.keys():
            self.channel_combo.addItem(str(x))
        self.channel_combo.selectionChanged.connect(self.request_channel_update) 
            
        layout.addWidget(self.channel_combo, row, 1)
        
        self.setLayout(layout)
    
    # Handle theme changes 
    def _update_icons(self): 
        self.browse_button.setIcon(qta.icon('fa6s.folder', color=get_qcolor('mint')))
        
    def event(self, event): 
        if event.type() == QEvent.Type.ApplicationPaletteChange: 
            self._update_icons()
        return super().event(event)
     
    def update_display_time(self):
        self.timeWindowChanged.emit(self.time_input.value())
    
    def update_grid(self):
        self.gridLayoutChanged.emit(self.rows_input.value(), self.cols_input.value())
    
    def request_channel_update(self, action, source): 
        if action == 'remove':
            self.removeChannelRequested.emit(source)
        elif action == 'add':
            self.addChannelRequested.emit(source)
        else:
            return 
        
    def update_channel(self, action, source):
        if action == 'add':
            self.channel_combo.select_channel(source)
        elif action == 'remove':
            self.channel_combo.unselect_channel(source)  
        else: 
            return None
    
    def openFolderDialog(self):
        folder = QFileDialog.getExistingDirectory(self, "Select Folder")
        if folder:  
            # If user didn't cancel the dialog
            self.folder_path.setText(folder)
            
    def update_param(self, param_name, value):
        self.config.set_param_value(param_name, value)
    
    def update_button_states(self, connection_status, running_status):
        if connection_status == ConnectionStatus.CONNECTED: 
            self.rows_input.setEnabled(running_status == RunningStatus.STOPPED)
            self.cols_input.setEnabled(running_status == RunningStatus.STOPPED)
            self.file_name.setEnabled(running_status == RunningStatus.STOPPED)
            self.folder_path.setEnabled(running_status == RunningStatus.STOPPED)
            self.browse_button.setEnabled(running_status == RunningStatus.STOPPED)
        else: 
            self.rows_input.setEnabled(True)
            self.cols_input.setEnabled(True)