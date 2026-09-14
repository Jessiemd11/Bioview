import darkdetect 
from PyQt6.QtGui import QColor

from bioview.constants import COLOR_SCHEME

def get_qcolor(name): 
    return QColor(*get_color_tuple(name))
    
def get_color_tuple(name): 
    if name not in COLOR_SCHEME.keys(): 
        print('Invalid color choice. Defaulting to blue')
        name = 'blue'
         
    if darkdetect.isDark():
        return COLOR_SCHEME[name]['dark']
    else: 
        return COLOR_SCHEME[name]['light']

def get_color_by_idx(idx=0):
    idx = idx % len(COLOR_SCHEME)
    return get_color_tuple(list(COLOR_SCHEME.keys())[idx])