import numpy as np

class TriangleGenerator:
    '''
    Generates a gated triangle wave burst that repeats every period_s.
    The burst lasts for num_triangles cycles of tri_freq_hz.

    rise_frac is the fraction of each cycle spent ramping from -1 up to +1;
    the rest is the fall back to -1. 0.5 is a symmetric triangle; anything
    else is an asymmetric (sawtooth-like) triangle, whose inverse is a
    time-reversed copy rather than a time-shifted one - so an inverted
    (flipped) received probe can be told apart from a delayed one.

    next(n) snapshots the current params at the top of the call, so changes
    made between calls take effect cleanly on the next chunk rather than
    mid-chunk.
    '''
    def __init__(self, fs, freq, n_tri, period_s, offset=0.0, amplitude=1.0, rise_frac=0.5):
        self.fs = float(fs)
        self.freq = max(float(freq), 1e-9)
        self.n_tri = max(int(n_tri), 1)
        self.period_s = max(float(period_s), 1e-6)
        self.offset = float(offset)
        self.amplitude = float(amplitude)
        self.rise_frac = min(max(float(rise_frac), 0.01), 0.99)
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
        w = self.rise_frac
        period_len = self.period_len
        burst_len = self.burst_len

        idx = self.sample_idx + np.arange(n, dtype=np.int64)
        pos = idx % period_len
        gate = pos < burst_len

        t_local = pos.astype(np.float64) / self.fs
        phase = t_local * freq
        p = phase - np.floor(phase)
        wave = np.where(p < w, 2.0 * p / w - 1.0, 1.0 - 2.0 * (p - w) / (1.0 - w))
        wave = wave * amp + offset

        out = np.zeros(n, dtype=np.float32)
        out[gate] = wave[gate].astype(np.float32)
        self.sample_idx += n
        return out, gate.astype(np.float32)
