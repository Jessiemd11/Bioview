'''
Ref: uhd examples
'''
import time 
import struct 
import numpy as np 
from pathlib import Path 
from datetime import datetime, timedelta

from bioview.constants import CLOCK_TIMEOUT

def _check_pairing(r_idx, t_idx, rx_cumsum, tx_cumsum, pair_list): 
    fn = lambda x, y: (np.where(x - y < 0))[0][0]
    rx_dev = fn(r_idx, rx_cumsum)
    tx_dev = fn(t_idx, tx_cumsum)
    
    return ((rx_dev, tx_dev) in pair_list) or ((tx_dev, rx_dev) in pair_list) or rx_dev == tx_dev

def get_channel_map(n_devices: int,
                    rx_per_dev: list,
                    tx_per_dev: list,
                    balance: bool = False,
                    multi_pairs: list = None
    ): 
    ''' 
    Provide base implementations of channel mappings for the following use-cases
    [1] MIMO 
    [2] Balance Signal 
    These two modifications are on top of the multi-band pairing 
    ''' 
    rx_cumsum = np.cumsum(rx_per_dev)
    tx_cumsum = np.cumsum(tx_per_dev)

    num_rxs = rx_cumsum[-1]
    num_txs = tx_cumsum[-1]
                    
    channel_map = [['' for _ in range(num_txs)] for _ in range(num_rxs)]
    
    if balance:
        rx_enabled = [True if r%2 == 0 else False for r in range(2*n_devices)]
        tx_enabled = [True if r%2 == 0 else False for r in range(2*n_devices)]
    else:
        rx_enabled = [True for _ in range(num_rxs)]
        tx_enabled = [True for _ in range(num_txs)]
    
    rx_ctr = 1
    
    for r_idx, rx_state in enumerate(rx_enabled):
        if not rx_state:
          continue
        
        tx_ctr = 1
        for t_idx, tx_state in enumerate(tx_enabled):
          if not tx_state:
            continue
          
          if multi_pairs is None or _check_pairing(r_idx, t_idx, rx_cumsum, tx_cumsum, multi_pairs): 
            channel_map[r_idx][t_idx] = f'Tx{tx_ctr}Rx{rx_ctr}'
          
          tx_ctr += 1
        
        rx_ctr += 1
    
    return channel_map

def setup_pps(usrp, pps, num_mboards):
    """Setup the PPS source."""
    if pps == "mimo":
        if num_mboards != 2:
            print('ref = "mimo" implies 2 motherboards; ' "your system has %d boards", num_mboards)
            return False
        # make mboard 1 a slave over the MIMO Cable
        usrp.set_time_source("mimo", 1)
    else:
        usrp.set_time_source(pps)
    return True

def setup_ref(usrp, ref, num_mboards):
    """Setup the reference clock."""
    if ref == "mimo":
        if num_mboards != 2:
            print('ref = "mimo" implies 2 motherboards; ' "your system has %d boards", num_mboards)
            return False
        usrp.set_clock_source("mimo", 1)
    else:
        usrp.set_clock_source(ref)

    # Lock onto clock signals for all mboards
    if ref != "internal":
        print("Now confirming lock on clock signals...")
        end_time = datetime.now() + timedelta(milliseconds=CLOCK_TIMEOUT)
        for i in range(num_mboards):
            if ref == "mimo" and i == 0:
                continue
            is_locked = usrp.get_mboard_sensor("ref_locked", i)
            while (not is_locked) and (datetime.now() < end_time):
                time.sleep(1e-3)
                is_locked = usrp.get_mboard_sensor("ref_locked", i)
            if not is_locked:
                print("Unable to confirm clock signal locked on board %d", i)
                return False
    return True

def check_channels(usrp, rx_channels, tx_channels):
    """Check that the device has sufficient RX and TX channels available."""
    # Check that each Rx channel specified is less than the number of total number of rx channels
    # the device can support
    dev_rx_channels = usrp.get_rx_num_channels()
    if not all(map((lambda chan: chan < dev_rx_channels), rx_channels)):
        print("Invalid RX channel(s) specified.")
        return [], []
        
    # Check that each Tx channel specified is less than the number of total number of tx channels
    # the device can support
    dev_tx_channels = usrp.get_tx_num_channels()
    if not all(map((lambda chan: chan < dev_tx_channels), tx_channels)):
        print("Invalid TX channel(s) specified.")
        return [], []
    
    return rx_channels, tx_channels

def read_file(fpath: str | Path, 
              channels: list,
              data_format: str = 'i8f'): 
    # Utility function to unpack stored files 
    buf_size = struct.calcsize(data_format)
    
    data_dict = {ch: [] for ch in channels}
    data_dict['Samples'] = []
     
    with open(fpath, 'rb') as f: 
        while True:
            data = f.read(buf_size)
            
            # Check for EOF
            if not data or len(data) < buf_size:
                print(f'Finished reading data')
                break
            
            # Unpack the data
            values = struct.unpack(data_format, data)
            
            # Store in a proper format
            data_dict['Samples'].append(values[0])
            for idx, ch in enumerate(channels): 
                data_dict[ch].append(complex(values[idx*2+1], values[idx*2+2])) 
    
    return data_dict