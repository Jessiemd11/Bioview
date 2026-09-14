import numpy as np

class TriangleGenerator:
    '''
    Generates a gated triangle wave burst that repeats every period_s.
    The burst lasts for num_triangles cycles of tri_freq_hz.

    next(n) snapshots the current params at the top of the call, so changes
    made between calls take effect cleanly on the next chunk rather than
    mid-chunk.
    '''
    def __init__(self, fs, freq, n_tri, period_s, offset=0.0, amplitude=1.0):
        self.fs = float(fs)
        self.freq = max(float(freq), 1e-9)
        self.n_tri = max(int(n_tri), 1)
        self.period_s = max(float(period_s), 1e-6)
        self.offset = float(offset)
        self.amplitude = float(amplitude)
        self.sample_idx = 0
        self._recalc()

    def _recalc(self):
        self.period_len = max(1, int(round(self.period_s * self.fs)))
        burst_samples = int(round(self.n_tri * self.fs / self.freq))
        self.burst_len = max(1, min(burst_samples, self.period_len))

    def next(self, n):
        freq = self.freq
        amp = self.amplitude
        offset = self.offset
        period_len = self.period_len
        burst_len = self.burst_len

        idx = self.sample_idx + np.arange(n, dtype=np.int64)
        pos = idx % period_len
        gate = pos < burst_len

        t_local = pos.astype(np.float64) / self.fs
        phase = t_local * freq
        wave = 2.0 * np.abs(2.0 * (phase - np.floor(phase + 0.5))) - 1.0
        wave = wave * amp + offset

        out = np.zeros(n, dtype=np.float32)
        out[gate] = wave[gate].astype(np.float32)
        self.sample_idx += n
        return out, gate.astype(np.float32)
