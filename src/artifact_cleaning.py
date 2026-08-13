import numpy as np
from scipy.signal import iirnotch, filtfilt
import spikeinterface.full as si
import spikeinterface.preprocessing as spre

def silence_saturation_events(recording, saturation_threshold=32000):
    """Detects ADC saturation events and zero-masks the affected time intervals."""
    traces = recording.get_traces()
    fs = recording.get_sampling_frequency()
    
    # Find indices exceeding saturation threshold
    saturated_samples = np.where(np.abs(traces) >= saturation_threshold)[0]
    
    if len(saturated_samples) == 0:
        return recording
        
    # Group contiguous sample blocks
    diffs = np.diff(saturated_samples)
    split_indices = np.where(diffs > 1)[0] + 1
    blocks = np.split(saturated_samples, split_indices)
    
    periods_sec = [(block[0] / fs, block[-1] / fs) for block in blocks if len(block) > 0]
    return spre.silence_periods(recording, list_of_periods=periods_sec, mode="zeros")

def remove_comb_noise(recording, channels, fundamental_freq=1000.0, q_factor=30.0):
    """Applies multi-harmonic notch filters to specific channels with periodic crosstalk."""
    fs = recording.get_sampling_frequency()
    nyquist = fs / 2.0
    harmonics = np.arange(fundamental_freq, nyquist, fundamental_freq)
    
    traces = recording.get_traces()
    cleaned_traces = traces.copy()
    
    for ch_idx in channels:
        ch_data = cleaned_traces[:, ch_idx]
        for freq in harmonics:
            b, a = iirnotch(w0=freq, Q=q_factor, fs=fs)
            ch_data = filtfilt(b, a, ch_data)
        cleaned_traces[:, ch_idx] = ch_data
        
    return si.NumpyRecording(cleaned_traces, sampling_frequency=fs)