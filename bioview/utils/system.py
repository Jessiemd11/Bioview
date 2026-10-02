import sys
import ctypes

# Windows constants (processthreadsapi.h / winbase.h)
_PROCESS_POWER_THROTTLING = 4                       # PROCESS_INFORMATION_CLASS.ProcessPowerThrottling
_PROCESS_POWER_THROTTLING_CURRENT_VERSION = 1
_PROCESS_POWER_THROTTLING_EXECUTION_SPEED = 0x1
_PROCESS_POWER_THROTTLING_IGNORE_TIMER_RESOLUTION = 0x4
_HIGH_PRIORITY_CLASS = 0x80

class _PowerThrottlingState(ctypes.Structure):
    _fields_ = [('Version', ctypes.c_ulong),
                ('ControlMask', ctypes.c_ulong),
                ('StateMask', ctypes.c_ulong)]

def boost_process():
    '''
    Keep Windows from starving the streaming threads: opt this process out of
    power throttling (EcoQoS / "efficiency mode", applied to background
    windows on Windows 11) and raise its priority class to High. Either can
    otherwise stall the Tx/Rx threads long enough to underflow/overflow the
    USRP, e.g. while another window is in front.
    Returns a list of (level, message) to log. No-op on other platforms.
    '''
    if sys.platform != 'win32':
        return []

    msgs = []
    from ctypes import wintypes
    kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
    # Explicit types - the default int return would truncate the 64-bit
    # pseudo-handle and every call would fail with ERROR_INVALID_HANDLE
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    kernel32.SetPriorityClass.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel32.SetPriorityClass.restype = wintypes.BOOL
    handle = kernel32.GetCurrentProcess()

    state = _PowerThrottlingState(
        _PROCESS_POWER_THROTTLING_CURRENT_VERSION,
        _PROCESS_POWER_THROTTLING_EXECUTION_SPEED | _PROCESS_POWER_THROTTLING_IGNORE_TIMER_RESOLUTION,
        0,  # StateMask 0 = never throttle
    )
    try:
        kernel32.SetProcessInformation.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
        kernel32.SetProcessInformation.restype = wintypes.BOOL
        ok = kernel32.SetProcessInformation(handle, _PROCESS_POWER_THROTTLING,
                                            ctypes.byref(state), ctypes.sizeof(state))
        if ok:
            msgs.append(('info', 'Windows power throttling disabled for BioView'))
        else:
            msgs.append(('warning', f'Could not disable Windows power throttling (error {ctypes.get_last_error()})'))
    except AttributeError:
        msgs.append(('warning', 'Windows power throttling control not available on this Windows version'))

    if kernel32.SetPriorityClass(handle, _HIGH_PRIORITY_CLASS):
        msgs.append(('info', 'BioView process priority set to High'))
    else:
        msgs.append(('warning', f'Could not raise process priority (error {ctypes.get_last_error()})'))

    return msgs
