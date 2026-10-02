# For some ass-backward reason, both uhd and time are ESSENTIAL for the app to not crash
import uhd
import time

import os
import queue
import logging

from PyQt6.QtWidgets import QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QStatusBar
from PyQt6.QtGui import QIcon, QGuiApplication
from PyQt6.QtCore import QMutex, Qt, QTimer

from bioview.components import UsrpDeviceConfigPanel, LogDisplayPanel, ExperimentSettingsPanel, PlotGrid, AppControlPanel, AnnotateEventPanel, DeviceStatusPanel, TextDialog, CalibrationPanel, CalibrationProbeDialog
from bioview.types import ConnectionStatus, RunningStatus, UsrpConfiguration, ExperimentConfiguration
from bioview.usrp import UsrpController, UsrpReceiver, UsrpTransmitter, CalibrationTransmitWorker
from bioview.common import SaveWorker, DisplayWorker, InstructionsWorker, CalibrationAnalyzer, write_calibration_result
from bioview.biopac import BiopacController
from bioview.utils import get_channel_map, boost_process

# The calibration analyzer is fed exactly the slice of the processed stream
# that belongs to its episode, located by raw-sample count rather than by
# wall-clock time: once the Save pipeline runs behind the radio (it did, by
# several seconds, late in a long recording), "whatever arrives while the
# calibration Tx is running" is stale pre-probe data and the probe itself is
# still sitting in the Rx queue.
# The window ends this long before the point the receivers had reached when
# the calibration Tx finished. The last burst is ~1 s (one INJ_PERIOD_S)
# before that point, so it is still included, while the next shadow-clock
# onset (which would otherwise be scored as a bogus extra burst) is not.
CAL_WINDOW_END_BEFORE_TX_DONE_S = 0.5
# Give up waiting for the Save pipeline to reach the end of the window
CAL_DRAIN_TIMEOUT_MS = 60000
    
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

        ### Calibration state - a short probe episode is injected into the
        ### same Tx/Rx/Save stream right after a recording starts and right
        ### before it stops (see start_recording/stop_recording), instead of
        ### calibration being its own separate session.
        self.calibration_enabled = False
        self.usrp_cal_tx_thread = [None] * len(self.usrp_config)
        self.calibration_analyzers = {}     # phase ('start'/'end') -> CalibrationAnalyzer
        self.calibration_results = {}       # phase -> {label: {...result, cvi, lut}}
        self._cal_phase = None              # phase currently in progress, or None
        self._cal_tx_finished_count = 0
        self._cal_start_sample = None       # raw-sample window fed to the analyzer
        self._cal_end_sample = None
        self._cal_episode_id = 0
        self._calibration_probe_dialog = None

        ### Make Connections
        self._connect_logging()

        ### Per-recording copy of the log panel, written next to the .h5
        self._log_file_handler = None

        ### Don't let Windows throttle the streaming threads
        for level, msg in boost_process():
            self.log_display_panel.log_message(level, msg)
    
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

        # Calibration Panel - compact single row, sits above the log
        cal_labels = [label for row in self.exp_config.channel_mapping for label in row if label]
        self.calibration_panel = CalibrationPanel(cal_labels)
        self.meta_panels.addWidget(self.calibration_panel, stretch=0)
        self.calibration_panel.calibrationEnabled.connect(self.update_calibration_state)

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

        self.calibration_analyzers = {}
        self.calibration_results = {}
        self._cal_phase = None

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

        # Start saving thread - one continuous file for the whole recording;
        # any calibration probe bursts injected below end up in this same
        # file/stream, since they really are injected into the same antenna.
        self.save_thread = SaveWorker(
            exp_config = self.exp_config,
            usrp_config = self.usrp_config,
            rx_queues = self.rx_queues,
            disp_queue = self.disp_queue,
            running = self.running_status.value,
            saving = self.saving_status
        )
        self.save_thread.logEvent.connect(self.log_display_panel.log_message)
        self.save_thread.data_ready.connect(self._feed_calibration)
        self.save_thread.start()
        # Annotation files (if any get created) share the data file's name
        self.annotate_event_panel.set_output_path(self.save_thread.out_file)
        if self.saving_status:
            self._open_log_file(self.save_thread.out_file)

        # Start instructions
        if self.enable_instructions:
            self.instructions_thread = InstructionsWorker(config = self.exp_config)
            self.instructions_thread.textUpdate.connect(self.instruction_dialog.update_instruction_text)
            self.instructions_thread.logEvent.connect(self.log_display_panel.log_message)
            self.instructions_thread.start()

        if self.calibration_enabled:
            # Normal Tx doesn't start yet - the calibration probe is injected
            # first (once Rx/Save are already running), and normal Tx takes
            # over once that episode finishes. 0.2s settle window against
            # startup transients before injecting the first burst.
            QTimer.singleShot(200, lambda: self._begin_calibration_episode('start'))
        else:
            self._start_normal_transmit()

        # Update UI
        self.update_buttons()

    def _open_log_file(self, data_path):
        self._close_log_file()
        # '_log.txt', not '.log' - the annotation panel already uses <stem>.log
        log_path = os.path.splitext(str(data_path))[0] + '_log.txt'
        # delay=True: the file is only created once something is logged
        handler = logging.FileHandler(log_path, encoding='utf-8', delay=True)
        handler.setFormatter(self.log_display_panel.log_handler.formatter)
        self.logger.addHandler(handler)
        self._log_file_handler = handler

    def _close_log_file(self):
        if self._log_file_handler is not None:
            self.logger.removeHandler(self._log_file_handler)
            self._log_file_handler.close()
            self._log_file_handler = None

    def _start_normal_transmit(self):
        for idx, cfg in enumerate(self.usrp_config):
            self.usrp_tx_thread[idx] = UsrpTransmitter(
                config = cfg,
                usrp = self.usrps[idx],
                tx_streamer = self.tx_streamers[idx]
            )
            self.usrp_tx_thread[idx].logEvent.connect(self.log_display_panel.log_message)
            self.usrp_tx_thread[idx].start()

    def _begin_calibration_episode(self, phase):
        # Guards against a Stop click landing in the 0.2s settle window
        if self.running_status != RunningStatus.RUNNING:
            return

        self.calibration_panel.set_running(True)

        # Construct the calibration Tx workers (and pay their waveform
        # precompute cost, which generates a ~1s buffer per Tx channel) BEFORE
        # marking the start of the analyzer's window. The analyzer's shadow
        # TriangleGenerator starts its own "period 0" clock at the first chunk
        # it is fed - if Tx construction cost sits between marking that point
        # and .start(), it directly adds to the gap between "analyzer thinks
        # period 0 starts" and "Tx actually starts sending" (realigned()
        # recovers from that, but keep it small). Keep .start() as the very
        # last step.
        self._cal_phase = phase
        self._cal_tx_finished_count = 0
        self._cal_start_sample = None
        self._cal_end_sample = None
        self._cal_episode_id += 1
        for idx, cfg in enumerate(self.usrp_config):
            self.usrp_cal_tx_thread[idx] = CalibrationTransmitWorker(
                config=cfg,
                usrp=self.usrps[idx],
                tx_streamer=self.tx_streamers[idx],
                num_cycles=3,
            )
            self.usrp_cal_tx_thread[idx].logEvent.connect(self.log_display_panel.log_message)
            self.usrp_cal_tx_thread[idx].finished.connect(self._on_calibration_tx_finished)

        analyzer = CalibrationAnalyzer(self.exp_config, save_iq=self.save_thread.save_iq)
        self.calibration_analyzers[phase] = analyzer
        # From here on _feed_calibration passes processed data to the analyzer
        self._cal_start_sample = self._rx_samples_received()

        for cal_tx_thread in self.usrp_cal_tx_thread:
            cal_tx_thread.start()

    def _rx_samples_received(self):
        counts = [rx.total_samps_received for rx in self.usrp_rx_thread if rx is not None]
        return min(counts) if counts else 0

    def _feed_calibration(self, payload):
        if self._cal_phase is None or self._cal_start_sample is None:
            return
        analyzer = self.calibration_analyzers.get(self._cal_phase)
        if analyzer is None or payload['end_sample'] <= self._cal_start_sample:
            return   # data received before the episode started (pipeline backlog)
        if self._cal_end_sample is None or payload['start_sample'] < self._cal_end_sample:
            analyzer.add_chunk(payload)
        if self._cal_end_sample is not None and payload['end_sample'] >= self._cal_end_sample:
            self._finalize_calibration_episode()

    def _on_calibration_tx_finished(self):
        self._cal_tx_finished_count += 1
        if self._cal_tx_finished_count < len(self.usrp_config):
            return   # wait for every device's calibration episode to finish

        for cal_tx_thread in self.usrp_cal_tx_thread:
            if cal_tx_thread is not None:
                cal_tx_thread.wait()

        # Don't analyze yet - the probe may still be queued behind a backlog in
        # the Save pipeline. _feed_calibration finalizes once processing gets
        # to the end of the episode's window.
        samp_rate = self.exp_config.samp_rate
        self._cal_end_sample = max(
            self._cal_start_sample + 1,
            self._rx_samples_received() - int(CAL_WINDOW_END_BEFORE_TX_DONE_S * samp_rate))
        lag_s = (self._cal_end_sample - self.save_thread.samples_processed) / samp_rate
        if lag_s <= 0:
            # Already processed (and fed) past the end of the window
            self._finalize_calibration_episode()
            return
        if lag_s > 1.0:
            self.log_display_panel.log_message('info', f'Calibration: waiting for {lag_s:.1f} s of queued data to be processed')
        episode_id = self._cal_episode_id
        QTimer.singleShot(CAL_DRAIN_TIMEOUT_MS, lambda: self._on_calibration_drain_timeout(episode_id))

    def _on_calibration_drain_timeout(self, episode_id):
        if episode_id != self._cal_episode_id or self._cal_phase is None or self._cal_end_sample is None:
            return   # already finalized (or aborted)
        self.log_display_panel.log_message('warning', 'Calibration: timed out waiting for the Save pipeline - analyzing the data received so far')
        self._finalize_calibration_episode()

    def _finalize_calibration_episode(self):
        phase = self._cal_phase
        analyzer = self.calibration_analyzers[phase]
        self._cal_phase = None
        self._cal_start_sample = None
        self._cal_end_sample = None

        # The analyzer's live shadow clock is anchored on a stale first chunk;
        # re-anchor on where the probe really is and replay.
        analyzer = analyzer.realigned()
        self.calibration_analyzers[phase] = analyzer

        result = analyzer.result()
        self.calibration_results[phase] = {
            label: dict(res, cvi=analyzer.compute_cvi(label), lut=analyzer.compute_lut(label))
            for label, res in result.items()
        }
        self.calibration_panel.show_result(result)

        if phase == 'start':
            self._start_normal_transmit()
        else:
            self._finish_stop_recording()

    def _abort_calibration_threads(self):
        for cal_tx_thread in self.usrp_cal_tx_thread:
            if cal_tx_thread is not None:
                cal_tx_thread.stop()
                cal_tx_thread.wait()
        # Stops _feed_calibration and any pending drain timeout
        self._cal_phase = None
        self._cal_start_sample = None
        self._cal_end_sample = None

    def stop_recording(self):
        if self.calibration_enabled and self.running_status == RunningStatus.RUNNING:
            # Stop normal Tx only - Rx/Save/Display keep running for the
            # end-of-recording calibration episode, which shares this same file.
            for tx_thread in self.usrp_tx_thread:
                if tx_thread is not None:
                    tx_thread.stop()
                    tx_thread.wait()
            self._begin_calibration_episode('end')
            return

        self._finish_stop_recording()

    def _finish_stop_recording(self):
        # Update state
        self.running_status = RunningStatus.STOPPED
        self.connection_status = ConnectionStatus.CONNECTED

        # Stop receiving threads first and let them exit, so that the Save
        # thread can then flush everything they queued
        for rx_thread in self.usrp_rx_thread:
            if rx_thread is not None:
                rx_thread.stop()
        for rx_thread in self.usrp_rx_thread:
            if rx_thread is not None:
                rx_thread.wait()

        # Stop instruction
        if self.instructions_thread is not None:
            self.instructions_thread.stop()

        # Stop saving thread - it processes the remaining queued data and
        # closes the file before exiting
        if self.save_thread is not None:
            self.save_thread.stop()
            self.save_thread.wait()

        # Stop display thread
        if self.display_thread is not None:
            self.display_thread.stop()

        # Stop streaming threads (already stopped in the calibration-enabled
        # path above, but harmless / still needed for the non-calibration path)
        for tx_thread in self.usrp_tx_thread:
            if tx_thread is not None:
                tx_thread.stop()

        # Update UI
        self.update_buttons()

        if 'start' in self.calibration_results and 'end' in self.calibration_results:
            self._show_calibration_outputs()

        # After the calibration JSON message, so it is in the file too
        self._close_log_file()

    def _show_calibration_outputs(self):
        start_analyzer = self.calibration_analyzers.get('start')
        end_analyzer = self.calibration_analyzers.get('end')
        start_view = start_analyzer.probe_view_sample() if start_analyzer is not None else {}
        end_view = end_analyzer.probe_view_sample() if end_analyzer is not None else {}

        self._calibration_probe_dialog = CalibrationProbeDialog(start_view, end_view, parent=self)
        self._calibration_probe_dialog.show()

        try:
            out_path = write_calibration_result(self.exp_config, self.usrp_config, self.calibration_results)
            self.log_display_panel.log_message('debug', f'Calibration results saved to {out_path}')
        except Exception as e:
            self.log_display_panel.log_message('error', f'Failed to save calibration results: {e}')

        # Consume the results so a later _finish_stop_recording (e.g. from
        # closeEvent) doesn't write a duplicate _calibration_2.json.
        self.calibration_results = {}

    def update_calibration_state(self, flag):
        self.calibration_enabled = flag

    def balance_signals(self):
        # TODO: Implement signal balancing here 
        pass 
      
    def update_buttons(self):
        self.app_control_panel.update_button_states(self.connection_status, self.running_status)
        self.experiment_settings_panel.update_button_states(self.connection_status, self.running_status)
        self.calibration_panel.update_button_states(self.connection_status, self.running_status)
    
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
        # Ensure all threads are closed - hard-abort rather than trying to
        # run a graceful end-of-recording calibration episode on app close.
        self._abort_calibration_threads()
        self._finish_stop_recording()
        self.annotate_event_panel.shutdown()
        return super().closeEvent(a0)
