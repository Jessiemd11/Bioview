# For some ass-backward reason, both uhd and time are ESSENTIAL for the app to not crash
import uhd
import time

import os 
import queue
import logging 

from PyQt6.QtWidgets import QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QStatusBar
from PyQt6.QtGui import QIcon, QGuiApplication
from PyQt6.QtCore import QMutex, Qt

from bioview.components import UsrpDeviceConfigPanel, LogDisplayPanel, ExperimentSettingsPanel, PlotGrid, AppControlPanel, AnnotateEventPanel, DeviceStatusPanel, TextDialog, CalibrationPanel
from bioview.types import ConnectionStatus, RunningStatus, UsrpConfiguration, ExperimentConfiguration
from bioview.usrp import UsrpController, UsrpReceiver, UsrpTransmitter, CalibrationTransmitWorker
from bioview.common import SaveWorker, DisplayWorker, InstructionsWorker, CalibrationAnalyzer, write_calibration_result
from bioview.biopac import BiopacController
from bioview.utils import get_channel_map
    
class Viewer(QMainWindow):
    def __init__(self, 
                 exp_config: ExperimentConfiguration, 
                 usrp_config: list[UsrpConfiguration] = None,
                 bio_config = None
    ):
        super().__init__()
        self.mutex = QMutex()
        
        ### Load configurations
        self.exp_config = exp_config
        self.usrp_config = usrp_config
        self.bio_config = bio_config
        
        ### Process configurations  
        self.source_counter = 0 
        self._generate_usrp_mappings()
        self._generate_biopac_mappings()
        
        ### Track state 
        self.connection_status = ConnectionStatus.DISCONNECTED
        self.running_status = RunningStatus.NOINIT
        self.saving_status = False 
        
        ### Track instruction
        if exp_config.get_param('instruction_type') is None:
            self.enable_instructions = False
        else: 
            self.enable_instructions = True 
        
        self.instruction_dialog = None     
        if exp_config.get_param('instruction_type') == 'text':
            self.instruction_dialog = TextDialog() 
        
        ### Populate list of devices 
        self.devices = {} 
        for cfg in self.usrp_config: 
            self.devices[cfg.device_name] = ConnectionStatus.DISCONNECTED
        if bio_config is not None:
            self.devices[bio_config.device_name] = ConnectionStatus.DISCONNECTED
        
        ### Set up UI
        self._init_ui()
        
        ### USRP Specific Variables
        self.usrps = [None] * len(self.usrp_config) 
        self.tx_streamers = [None] * len(self.usrp_config) 
        self.rx_streamers = [None] * len(self.usrp_config) 
        
        ### BIOPAC Specific Variables 
        self.biopac = None
        
        ### Threads
        # USRP
        self.usrp_init_thread = [None] * len(self.usrp_config) 
        self.usrp_tx_thread = [None] * len(self.usrp_config) 
        self.usrp_rx_thread = [None] * len(self.usrp_config) 
        # BIOPAC
        self.bio_init_thread = None 
        self.bio_rx_thread = None 
        # Common
        self.save_thread = None
        self.display_thread = None
        self.instructions_thread = None

        ### Data Queues
        self.rx_queues = [queue.Queue() for _ in range(len(self.usrp_config))]
        self.disp_queue = queue.Queue(maxsize=10000)
        self.cal_disp_queue = queue.Queue(maxsize=10000)

        ### Calibration state - fully separate from recording (mutually exclusive), but reuses
        ### self.rx_queues plus its own SaveWorker/DisplayWorker instances so calibration also
        ### gets live plots and (when Save? is checked) an .h5 file, same as a normal recording
        self.calibrating = False
        self.usrp_cal_tx_thread = [None] * len(self.usrp_config)
        self.usrp_cal_rx_thread = [None] * len(self.usrp_config)
        self.cal_save_thread = None
        self.cal_display_thread = None
        self.calibration_analyzer = None

        ### Make Connections
        self._connect_logging()
    
    def _init_ui(self): 
        ### Define main wndow
        self.setWindowTitle('BioView')
        scriptDir = os.path.dirname(os.path.realpath(__file__))
        self.setWindowIcon(QIcon(os.path.join(scriptDir, 'assets', 'icon.png')))
        screen = QGuiApplication.primaryScreen().geometry()
        width = screen.width()
        height = screen.height()
        self.setGeometry(int(0.2 * width), int(0.1 * height), int(0.6 * width), int(0.8 * height))
        
        ### Create central widget and main layout
        central_widget = QWidget(self)
        self.setCentralWidget(central_widget)
        main_layout = QVBoxLayout(central_widget)
        
        ### Top shelf container
        top_layout = QHBoxLayout()
        
        ### All controls are in one container    
        controls_layout = QVBoxLayout() 
    
        ### Connect/Start/Stop/Balance Signal Buttons 
        self.app_control_panel = AppControlPanel()
        controls_layout.addWidget(self.app_control_panel, stretch=1)
        # Connect signal handlers
        self.app_control_panel.connectionInitiated.connect(self.start_initialization)
        self.app_control_panel.startRequested.connect(self.start_recording)
        self.app_control_panel.stopRequested.connect(self.stop_recording)
        self.app_control_panel.saveRequested.connect(self.update_save_state)
        self.app_control_panel.instructionsEnabled.connect(self.toggle_instructions)
        self.app_control_panel.balanceRequested.connect(self.balance_signals)

        ### Calibration Panel
        cal_labels = [label for row in self.exp_config.channel_mapping for label in row if label]
        self.calibration_panel = CalibrationPanel(cal_labels)
        controls_layout.addWidget(self.calibration_panel, stretch=1)
        self.calibration_panel.calibrationRequested.connect(self.start_calibration)

        experiment_layout = QHBoxLayout()
        
        ### Experiment Control Panel
        self.experiment_settings_panel = ExperimentSettingsPanel(self.exp_config)
        experiment_layout.addWidget(self.experiment_settings_panel, stretch=1)
        # Connect handlers
        self.experiment_settings_panel.timeWindowChanged.connect(self.handle_time_window_change)
        self.experiment_settings_panel.gridLayoutChanged.connect(self.handle_grid_layout_change)
        self.experiment_settings_panel.addChannelRequested.connect(self.handle_add_channel)
        self.experiment_settings_panel.removeChannelRequested.connect(self.handle_remove_channel)
        
        ### USRP Device Config Panel(s)
        self.usrp_config_panel = [None] * len(self.usrp_config)
        for idx, usrp_cfg in enumerate(self.usrp_config): 
            self.usrp_config_panel[idx] = UsrpDeviceConfigPanel(usrp_cfg)
            experiment_layout.addWidget(self.usrp_config_panel[idx], stretch=1)
            
        controls_layout.addLayout(experiment_layout, stretch=4)
        top_layout.addLayout(controls_layout, stretch=3)   
        
        ### Metadata Panels
        self.meta_panels = QVBoxLayout() 
        # Status Panel - Experiment Log goes here
        self.logger = logging.getLogger(__name__)
        self.logger.setLevel(logging.INFO)
        self.log_display_panel = LogDisplayPanel(logger=self.logger)
        self.meta_panels.addWidget(self.log_display_panel, stretch=3)
        
        # Annotation Panel
        self.annotate_event_panel = AnnotateEventPanel(self.exp_config)
        self.meta_panels.addWidget(self.annotate_event_panel, stretch=2)

        self.annotate_event_panel.logEvent.connect(self.log_display_panel.log_message)

        top_layout.addLayout(self.meta_panels, stretch=2)
        
        main_layout.addLayout(top_layout)
        
        ### Plot Grid - Initialized using config['disp_channels']
        self.plot_grid = PlotGrid(self.exp_config)
        main_layout.addWidget(self.plot_grid)
        
        # Init plot grid
        for channel in self.exp_config.get_param('disp_channels', []):
            self.handle_add_channel(channel)
        
        central_widget.setLayout(main_layout)
        
        ### Status Bar 
        self.status_bar = QStatusBar()
        self.setStatusBar(self.status_bar)
        self.device_status_panel = DeviceStatusPanel(self.devices)
        # Add device status panel to status bar (on the right side)
        self.status_bar.addPermanentWidget(self.device_status_panel)
        # Add some info text to status bar
        self.status_bar.showMessage("Ready")
          
    def _generate_usrp_mappings(self): 
        if self.usrp_config is None: 
            return
        
        ### [1] -  Generate channel internal:external mapping
        counter = 0 
        for idx, cfg in enumerate(self.usrp_config):
            self.usrp_config[idx].absolute_channel_nums = [counter + val for val in cfg.rx_channels]
            counter += len(cfg.rx_channels)
        
        ### [2] - Generate usrp channel pair labels 
        if getattr(self.exp_config, 'channel_mapping', None) is None: 
            num_usrp_devices = len(self.usrp_config)
            rx_per_usrp = [len(x.rx_channels) for x in self.usrp_config]
            tx_per_usrp = [len(x.tx_channels) for x in self.usrp_config]
            
            self.exp_config.channel_mapping = get_channel_map(
                n_devices = num_usrp_devices, 
                rx_per_dev = rx_per_usrp, 
                tx_per_dev = tx_per_usrp, 
                balance = getattr(self.exp_config, 'balance', False), 
                multi_pairs = getattr(self.exp_config, 'multi_pairs', None) 
            )
        
        ### [3] - Generate usrp channel pair labels:data queue index mapping
        ch_map = self.exp_config.channel_mapping
        for ridx in range(len(ch_map)):
            for tidx in range(len(ch_map[ridx])):
                label = ch_map[ridx][tidx]
                if label != '': 
                    self.exp_config.data_mapping[label] = self.source_counter
                    self.source_counter += 1
        
        ### [4] Add usrp Parameters
        # Store sampling rate
        self.exp_config.samp_rate = self.usrp_config[0].samp_rate
        # Populate list of all channel frequencies
        channel_ifs = [None] * len(self.exp_config.channel_mapping)
        for cfg in self.usrp_config:
            for idx, abs_idx in enumerate(cfg.absolute_channel_nums):  
                channel_ifs[abs_idx] = cfg.if_freq[idx]
        self.exp_config.channel_ifs = channel_ifs
        
    def _generate_biopac_mappings(self):
        if self.bio_config is None:
            return 
        
        ### Generate Biopac channel labels:data queue index mapping
        for ch in self.bio_config.channels: 
            label = f'{self.bio_config.device_name}_Ch{ch}'
            self.exp_config.data_mapping[label] = self.source_counter
            self.source_counter += 1
             
    def _connect_logging(self): 
        self.plot_grid.logEvent.connect(self.log_display_panel.log_message)
        for idx, panel in enumerate(self.usrp_config_panel): 
            panel.logEvent.connect(self.log_display_panel.log_message)
        
    def start_initialization(self):
        # Disable button during initialization
        self.running_status = RunningStatus.NOINIT
        self.connection_status = ConnectionStatus.CONNECTING
        self.update_buttons() 
         
        # USRP   
        for idx, config in enumerate(self.usrp_config):
            self.usrp_init_thread[idx] = UsrpController(config)  
            self.usrp_init_thread[idx].initSucceeded.connect(
                lambda usrp, tx_streamer, rx_streamer, idx=idx: self.on_usrp_init_success(
                    usrp=usrp, 
                    tx_streamer=tx_streamer, 
                    rx_streamer=rx_streamer, 
                    idx=idx
                )
            )
            self.usrp_init_thread[idx].initFailed.connect(self.on_init_failure)
            self.usrp_init_thread[idx].logEvent.connect(self.log_display_panel.log_message)
            
            dev_name = self.usrp_config[idx].device_name
            self.device_status_panel.update_device_state(device_name=dev_name, new_state=ConnectionStatus.CONNECTING)
            self.usrp_init_thread[idx].start()
            self.usrp_init_thread[idx].wait()

        # BIOPAC
        if self.bio_config is not None: 
            self.bio_init_thread = BiopacController(self.bio_config)
            self.bio_init_thread.initSucceeded.connect(self.on_bio_init_success)
            self.bio_init_thread.initFailed.connect(self.on_init_failure)
            self.bio_init_thread.logEvent.connect(self.log_display_panel.log_message)
            self.device_status_panel.update_device_state(device_name='BIOPAC', new_state=ConnectionStatus.CONNECTING)
            self.bio_init_thread.start()
            self.bio_init_thread.wait()
        
    def start_recording(self):
        # Update state 
        self.running_status = RunningStatus.RUNNING
        self.connection_status = ConnectionStatus.CONNECTED
        
        # This will set T=0 AND update the file path.
        self.annotate_event_panel.set_start_time()
        
        # Start streaming threads
        for idx, cfg in enumerate(self.usrp_config):
            self.usrp_tx_thread[idx] = UsrpTransmitter(
                config = cfg, 
                usrp = self.usrps[idx], 
                tx_streamer = self.tx_streamers[idx]
            )
            self.usrp_tx_thread[idx].logEvent.connect(self.log_display_panel.log_message)
            self.usrp_tx_thread[idx].start()
         
        # Start receiving threads
        for idx, cfg in enumerate(self.usrp_config):
            self.usrp_rx_thread[idx] = UsrpReceiver(
                usrp = self.usrps[idx], 
                config = cfg, 
                rx_streamer = self.rx_streamers[idx], 
                rx_queue = self.rx_queues[idx],
                running = self.running_status.value
            )
            self.usrp_rx_thread[idx].logEvent.connect(self.log_display_panel.log_message)
            self.usrp_rx_thread[idx].start()
        
        # Start display thread 
        self.display_thread = DisplayWorker(
            config = self.exp_config, 
            disp_queue = self.disp_queue, 
            running = self.running_status.value
        )
        self.display_thread.logEvent.connect(self.log_display_panel.log_message)
        self.display_thread.dataReady.connect(self.plot_grid.add_new_data)        
        self.display_thread.start()
        
        # Start saving thread 
        self.save_thread = SaveWorker(
            exp_config = self.exp_config, 
            usrp_config = self.usrp_config,
            rx_queues = self.rx_queues, 
            disp_queue = self.disp_queue, 
            running = self.running_status.value, 
            saving = self.saving_status
        )
        self.save_thread.logEvent.connect(self.log_display_panel.log_message)
        self.save_thread.start()
        
        # Start instructions
        if self.enable_instructions:
            self.instructions_thread = InstructionsWorker(config = self.exp_config)
            self.instructions_thread.textUpdate.connect(self.instruction_dialog.update_instruction_text)
            self.instructions_thread.logEvent.connect(self.log_display_panel.log_message)
            self.instructions_thread.start()
            
        # Start all threads together 
        
        
        # Update UI 
        self.update_buttons()
         
    def stop_recording(self):
        # Update state
        self.running_status = RunningStatus.STOPPED
        self.connection_status = ConnectionStatus.CONNECTED
        
        # Stop receiving threads
        for rx_thread in self.usrp_rx_thread:
            if rx_thread is not None: 
                rx_thread.stop()
        
        # Stop instruction 
        if self.instructions_thread is not None:
            self.instructions_thread.stop()
            
        # Stop saving thread
        if self.save_thread is not None: 
            self.save_thread.stop()
        
        # Stop display thread
        if self.display_thread is not None: 
            self.display_thread.stop()
        
        # Stop streaming threads
        for tx_thread in self.usrp_tx_thread:
            if tx_thread is not None: 
                tx_thread.stop()
    
        # Update UI 
        self.update_buttons()
     
    def balance_signals(self):
        # TODO: Implement signal balancing here 
        pass 
      
    def update_buttons(self):
        self.app_control_panel.update_button_states(self.connection_status, self.running_status)
        self.experiment_settings_panel.update_button_states(self.connection_status, self.running_status)
        self.calibration_panel.update_button_states(self.connection_status, self.running_status)

    def start_calibration(self):
        # Mutually exclusive with Recording - guarded here even though the button is already
        # disabled in that state, since update_button_states is the only other gate
        if self.calibrating or self.running_status == RunningStatus.RUNNING:
            return
        if self.connection_status != ConnectionStatus.CONNECTED:
            return

        self.calibrating = True
        self.calibration_panel.set_running(True)
        self.update_buttons()

        # Sets T=0 and the save-file path, same as a normal recording
        self.annotate_event_panel.set_start_time()

        for idx, cfg in enumerate(self.usrp_config):
            self.usrp_cal_tx_thread[idx] = CalibrationTransmitWorker(
                config=cfg,
                usrp=self.usrps[idx],
                tx_streamer=self.tx_streamers[idx],
                duration_s=30.0,
            )
            self.usrp_cal_tx_thread[idx].logEvent.connect(self.log_display_panel.log_message)
            if idx == 0:
                self.usrp_cal_tx_thread[idx].finished.connect(self.finish_calibration)
            self.usrp_cal_tx_thread[idx].start()

            self.usrp_cal_rx_thread[idx] = UsrpReceiver(
                usrp=self.usrps[idx],
                config=cfg,
                rx_streamer=self.rx_streamers[idx],
                rx_queue=self.rx_queues[idx],
                running=True,
            )
            self.usrp_cal_rx_thread[idx].logEvent.connect(self.log_display_panel.log_message)
            self.usrp_cal_rx_thread[idx].start()

        # Live plots during calibration, same widget as a normal recording
        self.cal_display_thread = DisplayWorker(
            config=self.exp_config,
            disp_queue=self.cal_disp_queue,
            running=True,
        )
        self.cal_display_thread.logEvent.connect(self.log_display_panel.log_message)
        self.cal_display_thread.dataReady.connect(self.plot_grid.add_new_data)
        self.cal_display_thread.start()

        # Same SaveWorker normal recording uses - writes an .h5 file when Save? is checked,
        # and (regardless of saving_status) emits data_ready for the calibration analyzer
        self.cal_save_thread = SaveWorker(
            exp_config=self.exp_config,
            usrp_config=self.usrp_config,
            rx_queues=self.rx_queues,
            disp_queue=self.cal_disp_queue,
            running=True,
            saving=self.saving_status,
        )
        self.cal_save_thread.logEvent.connect(self.log_display_panel.log_message)

        self.calibration_analyzer = CalibrationAnalyzer(self.exp_config, save_iq=self.cal_save_thread.save_iq)
        self.cal_save_thread.data_ready.connect(self.calibration_analyzer.add_chunk)
        self.cal_save_thread.start()

    def finish_calibration(self):
        if not self.calibrating:
            return

        for idx in range(len(self.usrp_config)):
            tx_thread = self.usrp_cal_tx_thread[idx]
            if tx_thread is not None:
                tx_thread.stop()
                tx_thread.wait()
                self.usrp_cal_tx_thread[idx] = None

            rx_thread = self.usrp_cal_rx_thread[idx]
            if rx_thread is not None:
                rx_thread.stop()
                rx_thread.wait()
                self.usrp_cal_rx_thread[idx] = None

        if self.cal_save_thread is not None:
            self.cal_save_thread.stop()
            self.cal_save_thread.wait()
            self.cal_save_thread = None

        if self.cal_display_thread is not None:
            self.cal_display_thread.stop()
            self.cal_display_thread.wait()
            self.cal_display_thread = None

        channels = self.calibration_analyzer.result() if self.calibration_analyzer is not None else {}
        try:
            out_path = write_calibration_result(self.exp_config, self.usrp_config, channels)
            self.log_display_panel.log_message('info', f'Calibration saved to {out_path}')
        except Exception as e:
            self.log_display_panel.log_message('error', f'Failed to save calibration results: {e}')

        self.calibration_panel.show_result(channels)
        self.calibration_analyzer = None
        self.calibrating = False
        self.calibration_panel.set_running(False)
        self.update_buttons()
    
    def on_usrp_init_success(self, usrp, tx_streamer, rx_streamer, idx=0):
        self.usrps[idx] = usrp
        self.tx_streamers[idx] = tx_streamer
        self.rx_streamers[idx] = rx_streamer
        
        # Update status bar
        dev_name = self.usrp_config[idx].device_name
        self.devices[dev_name] = ConnectionStatus.CONNECTED
        self.device_status_panel.update_device_state(device_name=dev_name, new_state=ConnectionStatus.CONNECTED)
        
        # If all devices have inited, do an overall status update
        self.on_init_success()
        
    def on_bio_init_success(self, biopac):
        self.biopac = biopac
        dev_name = self.bio_config.device_name
        self.devices[dev_name] = ConnectionStatus.CONNECTED
        self.device_status_panel.update_device_state(device_name=dev_name, new_state=ConnectionStatus.CONNECTED)
        
        # If all devices have inited, do an overall status update
        self.on_init_success()
        
    def on_init_success(self): 
        self.mutex.lock()
        inited = True
        for dev_name, dev_state in self.devices.items(): 
            if dev_state != ConnectionStatus.CONNECTED: 
                inited = False 
                break  
        
        if inited:    
            self.running_status = RunningStatus.STOPPED
            self.connection_status = ConnectionStatus.CONNECTED
            self.update_buttons() 
            
        self.mutex.unlock()
        
    def on_init_failure(self, error_message):
        self.running_status = RunningStatus.NOINIT
        self.connection_status = ConnectionStatus.DISCONNECTED
        self.update_buttons() 

        self.log_display_panel.log_message('error', error_message)
        self.usrp = None
    
    def handle_time_window_change(self, seconds):
        self.plot_grid.set_display_time(seconds)
    
    def handle_grid_layout_change(self, rows, cols):
        self.plot_grid.update_grid(rows, cols)
    
    def handle_add_channel(self, channel):
        if self.plot_grid.add_channel(channel):
            # Update config 
            sel_channels = self.exp_config.get_param('disp_channels')
            sel_channels.append(channel)
            self.exp_config.set_param_value('disp_channels', list(set(sel_channels)))
            # Change state of UI 
            self.experiment_settings_panel.update_channel('add', channel)
    
    def handle_remove_channel(self, channel):
        if self.plot_grid.remove_channel(channel):
            # Update config 
            sel_channels = self.exp_config.get_param('disp_channels')
            sel_channels.remove(channel)
            self.exp_config.set_param_value('disp_channels', sel_channels)
            # Change state of UI 
            self.experiment_settings_panel.update_channel('remove', channel)
    
    def update_save_state(self, flag): 
        self.saving_status = flag
        
    def toggle_instructions(self, flag):
        self.enable_instructions = flag
        if self.instruction_dialog is not None: 
            self.instruction_dialog.toggle_ui(self.enable_instructions)
        
    def closeEvent(self, a0):
        # Ensure all threads are closed
        self.stop_recording()
        return super().closeEvent(a0)


    def closeEvent(self, a0):
        self.stop_recording()
        self._abort_calibration_threads()
        self.annotate_event_panel.shutdown()
        return super().closeEvent(a0)

    def _abort_calibration_threads(self):
        # Used on app close mid-calibration - just tears threads down, no result/save
        if not self.calibrating:
            return
        for idx in range(len(self.usrp_config)):
            if self.usrp_cal_tx_thread[idx] is not None:
                self.usrp_cal_tx_thread[idx].stop()
                self.usrp_cal_tx_thread[idx].wait()
            if self.usrp_cal_rx_thread[idx] is not None:
                self.usrp_cal_rx_thread[idx].stop()
                self.usrp_cal_rx_thread[idx].wait()
        if self.cal_save_thread is not None:
            self.cal_save_thread.stop()
            self.cal_save_thread.wait()
        if self.cal_display_thread is not None:
            self.cal_display_thread.stop()
            self.cal_display_thread.wait()
        self.calibrating = False

    

