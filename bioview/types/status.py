from enum import Enum 
from bioview.utils import get_qcolor

# Handle connection as an enum for better clarity
class ConnectionStatus(Enum): 
    DISCONNECTED = ('Not Connected', get_qcolor('red'))
    CONNECTING = ('Connecting', get_qcolor('yellow'))
    CONNECTED = ('Connected', get_qcolor('green'))

class RunningStatus(Enum):
    NOINIT = False
    RUNNING = True
    STOPPED = False

# Per-channel-pair calibration quality, shown as traffic-light LEDIndicators
class ChannelQualityStatus(Enum):
    PENDING = ('Pending', get_qcolor('grey'))
    GOOD = ('Good', get_qcolor('green'))
    MARGINAL = ('Marginal', get_qcolor('orange'))
    POOR = ('Poor', get_qcolor('red'))
