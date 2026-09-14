import json 
from pathlib import Path

def _get_cache_file(file_name): 
    cache_file = Path.home() / '.bioview' / file_name
    
    if not cache_file.exists():
        cache_file.parent.mkdir(parents=False, exist_ok=True)
        cache_file.touch()
    
    return cache_file
    
def get_usrp_address(device_name: str): 
    cache_file = _get_cache_file('serial_maps')
    map_dict = {}
    
    try: 
        with open(cache_file, 'r') as fobj: 
            map_dict = json.load(fobj)
    except Exception as e:
        print('Cache is empty')
        return None
        
    return map_dict[device_name]

def update_usrp_address(device_name: str,
                        device_serial: str): 
    cache_file = _get_cache_file('serial_maps')
    map_dict = {}
    
    try: 
        with open(cache_file, 'r') as fobj: 
            map_dict = json.load(fobj)
    except Exception as e:
        print('Cache does not exist currently. Creating new cache file.')
    
    map_dict[device_name] = device_serial
    
    # Update 
    try: 
        with open(cache_file, 'w') as fobj: 
            json.dump(map_dict, fobj)
    except Exception as e:
        print(f'Error updating cache: {e}')
    finally:
        print('Cache updated successfully')