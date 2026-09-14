import queue
import numpy as np
from collections import deque
import pyqtgraph as pg

from PyQt6.QtWidgets import QWidget, QGridLayout
from PyQt6.QtCore import pyqtSignal, QTimer, QEvent
from PyQt6.QtGui import QGuiApplication

from bioview.utils import get_color_by_idx

class PlotManager(): 
    def __init__(self, 
                disp_freq: int, 
                color: str,                  
                max_buffer_size: int, 
                display_duration: float, 
                data_src: str = '',
                xlabel: str = 'Time (s)',
                ylabel: str = 'Amplitude'
    ):
        # UI widget 
        self.widget = pg.PlotWidget()
        self.widget.setAntialiasing(False)
        self.widget.getPlotItem().setDownsampling(auto=True, mode='subsample')
        self.widget.enableAutoRange(pg.ViewBox.YAxis, enable=True)  
        self.widget.enableAutoRange(pg.ViewBox.XAxis, enable=False)
        self.widget.setMouseEnabled(x=False, y=False)
        self.widget.setBackground(None) 
        self.widget.showGrid(x=True, y=True)
        self.widget.setLabel('bottom', xlabel)
        self.widget.setLabel('left', ylabel)
        
        # Create pen and plot item ONCE - this is key for performance
        self.pen = pg.mkPen(color=color, width=1)
        self.plot_item = self.widget.plot([], [], pen=self.pen)
        
        # Plot specs
        self.max_buffer_size = max_buffer_size
        self.display_duration = display_duration
        self.disp_freq = disp_freq
        
        # Data handling 
        self.data_src = data_src
        self.data_queue = queue.Queue()
        
        # Initialize after setting up basic properties
        self._init_plot()
        
    def _init_plot(self): 
        # Calculate number of points for the display duration
        self.num_points = int(self.display_duration * self.disp_freq)
        
        # Initialize buffer with zeros - deque with fixed maxlen for sliding window
        self.buffer = deque([0.0] * self.num_points, maxlen=self.num_points)
        
        # Create time vector that will be reused
        self.time_vector = np.linspace(0, self.display_duration, self.num_points, endpoint=False)
        
        # Set initial data on the plot item (don't create new plot)
        self.plot_item.setData(self.time_vector, list(self.buffer))
        
        # Set ranges correctly
        self.widget.setXRange(0, self.display_duration, padding=0)
        
    def update_data_source(self, data_src: str = ''): 
        self.widget.setTitle(data_src)
        self.data_src = data_src
        self._init_plot()
        
        # Flush queue 
        while not self.data_queue.empty():
            try:
                self.data_queue.get_nowait()
            except queue.Empty:
                break
    
    def add_data(self, data): 
        """Add new data to the queue with overflow protection"""
        if isinstance(data, (list, np.ndarray)):
            # If it's an array, add each point individually to maintain proper sliding
            for point in np.atleast_1d(data):
                self._add_single_point(float(point))
        else:
            self._add_single_point(float(data))
    
    def _add_single_point(self, point):
        """Add a single point with queue size management"""
        # Keep queue size reasonable - if it gets too big, we're falling behind
        max_queue_size = self.num_points // 4  # Allow max 25% of display buffer in queue
        
        if self.data_queue.qsize() > max_queue_size:
            # We're falling behind - drop old data to stay real-time
            try:
                # Drop some old points to make room
                drop_count = max_queue_size // 4
                for _ in range(drop_count):
                    self.data_queue.get_nowait()
            except queue.Empty:
                pass
        
        self.data_queue.put(point)
        
    def update_plot(self): 
        # Calculate how many points we should process to stay real-time
        queue_size = self.data_queue.qsize()
        
        if queue_size == 0:
            return
            
        # If queue is getting large, process more aggressively to catch up
        if queue_size > self.num_points // 8:  # More than 12.5% of display buffer
            # Process more points to catch up, but skip some to stay real-time
            points_to_process = min(queue_size, self.num_points // 4)
            skip_ratio = max(1, queue_size // points_to_process)  # Skip every Nth point
        else:
            # Normal processing
            points_to_process = min(queue_size, 50)  # Process reasonable chunk
            skip_ratio = 1  # Don't skip any points
        
        updates_made = False
        points_processed = 0
        skip_counter = 0
        
        while not self.data_queue.empty() and points_processed < points_to_process:
            try:
                new_point = self.data_queue.get_nowait()
                
                # Skip points if we're behind (temporal downsampling)
                skip_counter += 1
                if skip_counter >= skip_ratio:
                    skip_counter = 0
                    # Add to deque - this automatically removes oldest point due to maxlen
                    self.buffer.append(new_point)
                    updates_made = True
                
                points_processed += 1
                
            except queue.Empty:
                break
        
        # Only update the plot if we have new data
        if updates_made:
            # Convert deque to list for plotting - this is the sliding window effect
            self.plot_item.setData(self.time_vector, list(self.buffer))

    def update_display_duration(self, duration): 
        self.display_duration = duration
        self._init_plot()

class PlotGrid(QWidget):
    logEvent = pyqtSignal(str, str)
    
    def __init__(self, 
                 config, 
                 parent = None):
        super().__init__(parent)
        self.config = config
        
        self.rows = 2
        self.cols = 2
        self.display_duration = 10.0 
        
        self.disp_freq = config.samp_rate / (config.save_ds * config.disp_ds)
        self.selected_channels = {} 
        
        # Set up the layout
        self.layout = QGridLayout(self)
        self.layout.setContentsMargins(0, 0, 0, 0)
        self.layout.setSpacing(5)    
        
        # Keep track of available plots that are not connected to an output 
        self.available_slots = []
        
        # Optimize refresh rate and ensure real-time performance
        self.refresh_time = max(self._get_monitor_refresh_delay(), 10)  # Max 60 FPS
        self.update_timer = QTimer()
        self.update_timer.timeout.connect(self.update_plots)
        
        # Add queue monitoring for debugging
        self.queue_monitor_timer = QTimer()
        self.queue_monitor_timer.timeout.connect(self._monitor_queues)
        self.queue_monitor_timer.start(2000)  # Check every 2 seconds
        
        # Initialize grid
        self.init_grid()
        self.update_timer.start(self.refresh_time)
        
    def _get_monitor_refresh_delay(self):
        screen = QGuiApplication.primaryScreen()
        if screen:
            return int(1000 // screen.refreshRate())
        else:
            return 16 # 60 Hz by default 

    # Handle theme changes     
    def event(self, event): 
        if event.type() == QEvent.Type.PaletteChange: 
            for r in range(self.rows):
                for c in range(self.cols): 
                    self.plots[r][c].widget.setBackground(None) 
                    self.plots[r][c].pen = pg.mkPen(color=get_color_by_idx(r*self.cols + c), width=1)
        return super().event(event)
    
    def init_grid(self):
        self.plots = [[None for _ in range(self.cols)] for _ in range(self.rows)] 
        
        for r in range(self.rows):
            for c in range(self.cols):
                plot_obj = PlotManager(
                    disp_freq=self.disp_freq,
                    color = get_color_by_idx(r*self.cols + c),
                    max_buffer_size = max(10, self.refresh_time // 10),  # Reasonable buffer size
                    display_duration = self.display_duration        
                )
                
                self.layout.addWidget(plot_obj.widget, r, c)
                self.plots[r][c] = plot_obj
                
                # Initially, all slots are available 
                self.available_slots.append((r, c))
    
    def update_grid(self, rows, cols):
        # Clear past grid 
        while(self.layout.count()): 
            child = self.layout.takeAt(0)
            if child.widget(): 
                child.widget().deleteLater()
        
        # Re-initialize 
        self.rows = rows 
        self.cols = cols 
        self.selected_channels = {} 
        
        # Flush queue of available slots 
        self.available_slots = []
        
        self.init_grid()
    
    def add_channel(self, channel):
        if channel in self.selected_channels.keys():
            self.logEvent.emit('debug', 'Unable to add channel as it is already being plotted')
            return True
    
        try: 
            # Sort
            self.available_slots.sort(key=lambda x: x[0] * self.cols + x[1])
            # Pop
            row, col = self.available_slots.pop(0)
        except IndexError:
            self.logEvent.emit('warning', 'All graph slots full. Update layout or remove an existing trace.')
            return False 
        
        plot_obj = self.plots[row][col]
        plot_obj.update_data_source(channel)
        
        # Update selected channel mapping
        self.selected_channels[channel] = {
            'plot': plot_obj,
            'loc': (row, col)
        }
        
        return True
    
    def remove_channel(self, channel):
        if channel not in self.selected_channels.keys():
            self.logEvent.emit('debug', 'Unable to remove channel as it is not being plotted')
            return
            
        # Clear the plot
        plot_obj = self.selected_channels[channel]['plot']
        loc = self.selected_channels[channel]['loc']
        
        plot_obj.update_data_source()
        
        # Remove from data structures
        self.selected_channels.pop(channel, None)
        self.available_slots.append(tuple(loc))
        
        return True
    
    def add_new_data(self, data): 
        for channel_idx in range(np.shape(data)[0]): 
            if channel_idx >= len(self.config.disp_channels):
                break
                
            channel_key = self.config.disp_channels[channel_idx]
            if channel_key not in self.selected_channels.keys(): 
                continue 
                
            plot_obj = self.selected_channels[channel_key]['plot']
            # Pass the data for this channel (could be multiple samples)
            plot_obj.add_data(data[channel_idx, :])

    def update_plots(self):
        for val in self.selected_channels.values():
            plot_obj = val['plot']
            plot_obj.update_plot()
    
    def _monitor_queues(self):
        total_queued = 0
        max_queue = 0
        
        for channel_data in self.selected_channels.values():
            plot_obj = channel_data['plot']
            queue_size = plot_obj.data_queue.qsize()
            total_queued += queue_size
            max_queue = max(max_queue, queue_size)
        
        if max_queue > 100:  # Threshold for concern
            self.logEvent.emit('debug', f'Plot queues getting large - max: {max_queue}, total: {total_queued}. Consider reducing data rate or increasing processing.')
        elif total_queued > 0:
            self.logEvent.emit('debug', f'Active plot queues - max: {max_queue}, total: {total_queued}')
            
    def set_display_time(self, dur):
        self.display_duration = dur
        for r in range(self.rows):
            for c in range(self.cols):
                self.plots[r][c].update_display_duration(dur)