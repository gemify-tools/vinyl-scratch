# SPDX-License-Identifier: GPL-3.0-only
import numpy as np
import sounddevice as sd
import threading
import dearpygui.dearpygui as dpg

# =========================
# Core audio configuration
# =========================
SAMPLE_RATE = 44100
CHANNELS = 2
BLOCK_SIZE = 4096
LATENCY = "high"

# Overall vinyl noise levels (defaults)
params = {
    "NOISE_FLOOR": 0.006,
    "CRACKLE_LEVEL": 0.015,
    # Pop / crackle events
    "MICRO_CRACKLE_RATE": 12.0,
    "POP_RATE": 1.0,
    "BURST_RATE": 0.08,
    "BURST_MEAN_LEN": 0.4,
    "POP_MIN_AMP": 0.08,
    "POP_MAX_AMP": 0.28,
    "POP_DECAY": 0.02,
    # Low-frequency turntable/room tone
    "RUMBLE_FREQ": 30.0,
    "RUMBLE_LEVEL": 0.009,
    # Warped vinyl wow parameters
    "WARP_RATE": 0.6,
    "WARP_DEPTH_MS": 3.0,
    # How much vinyl noise to add on top of music
    "VINYL_MIX": 0.25,
}

rng = np.random.default_rng(1234)

# State for pops / bursts
pop_tails = []  # list of (amp, remaining_samples)
burst_state = 0

# delay buffer for warped‑vinyl effect
MAX_DELAY_MS = 15.0
MAX_DELAY_SAMPLES = int(MAX_DELAY_MS * 0.001 * SAMPLE_RATE)
delay_buffer = np.zeros((MAX_DELAY_SAMPLES, CHANNELS), dtype=np.float32)
delay_index = 0
lfo_phase = 0.0

# Audio stream state
stream = None
running = False
audio_thread_handle = None

# =========================
# DSP
# =========================
def soft_clip(x, drive=1.6):
    return np.tanh(drive * x) / np.tanh(drive)

def generate_pop_block(frames):
    global pop_tails
    out = np.zeros((frames, CHANNELS), dtype=np.float32)

    # continue tails
    new_tails = []
    for amp, remaining in pop_tails:
        n = min(frames, remaining)
        t = np.arange(n, dtype=np.float32) / SAMPLE_RATE
        env = np.exp(-t / params["POP_DECAY"]).astype(np.float32)
        out[:n, :] += (amp * env)[:, None]
        remaining -= n
        if remaining > 0:
            # continue with correct exponential continuation
            tail_amp = amp * np.exp(-n / SAMPLE_RATE / params["POP_DECAY"])
            new_tails.append((tail_amp, remaining))
    pop_tails = new_tails

    # new pops
    block_duration = frames / SAMPLE_RATE
    lam = params["POP_RATE"] * block_duration
    num_pops = rng.poisson(lam)
    for _ in range(num_pops):
        start = int(rng.integers(0, frames))
        pop_len = int(params["POP_DECAY"] * SAMPLE_RATE)
        end = min(frames, start + pop_len)
        n = end - start
        if n <= 0:
            continue
        amp = float(rng.uniform(params["POP_MIN_AMP"], params["POP_MAX_AMP"]))
        t = np.arange(n, dtype=np.float32) / SAMPLE_RATE
        env = np.exp(-t / params["POP_DECAY"]).astype(np.float32)
        out[start:end, :] += (amp * env)[:, None]

        tail = start + pop_len - frames
        if tail > 0:
            tail_amp = amp * np.exp(-n / SAMPLE_RATE / params["POP_DECAY"])
            pop_tails.append((tail_amp, int(tail)))

    return out

def generate_micro_crackle(frames):
    out = np.zeros((frames, CHANNELS), dtype=np.float32)
    block_duration = frames / SAMPLE_RATE
    lam = params["MICRO_CRACKLE_RATE"] * block_duration
    num = rng.poisson(lam)
    for _ in range(num):
        idx = int(rng.integers(0, frames))
        width = int(rng.integers(1, 6))
        end = min(frames, idx + width)
        amp = float(rng.uniform(0.01, 0.07))
        tick = amp * (1.0 - np.linspace(0, 1, end - idx, dtype=np.float32))
        out[idx:end, :] += tick[:, None]
    return out

def generate_burst_mask(frames):
    global burst_state
    mask = np.ones(frames, dtype=np.float32)

    if burst_state > 0:
        n = min(frames, burst_state)
        mask[:n] *= 2.3
        burst_state -= n

    block_duration = frames / SAMPLE_RATE
    lam = params["BURST_RATE"] * block_duration
    if rng.random() < lam:
        burst_len = int(rng.exponential(params["BURST_MEAN_LEN"]) * SAMPLE_RATE)
        n = min(frames, burst_len)
        mask[:n] *= 2.3
        burst_state = max(0, burst_len - n)

    return mask

def generate_vinyl_noise(frames):
    white = rng.normal(
        0.0, params["NOISE_FLOOR"], size=(frames, CHANNELS)
    ).astype(np.float32)

    diff = np.zeros_like(white)
    diff[1:, :] = white[1:, :] - 0.7 * white[:-1, :]
    base = white + 0.35 * diff

    t = np.arange(frames, dtype=np.float32) / SAMPLE_RATE
    phase = 2 * np.pi * params["RUMBLE_FREQ"] * t
    rumble = (params["RUMBLE_LEVEL"] * np.sin(phase)).astype(np.float32)
    rumble = rumble[:, None].repeat(CHANNELS, axis=1)

    burst_mask = generate_burst_mask(frames)
    micro = generate_micro_crackle(frames)
    pops = generate_pop_block(frames)

    micro *= (params["CRACKLE_LEVEL"] * burst_mask[:, None])
    y = base + rumble + micro + pops
    return soft_clip(y, drive=1.5).astype(np.float32)

def apply_warped_vinyl(in_block):
    global delay_buffer, delay_index, lfo_phase
    frames = in_block.shape[0]
    out = np.zeros_like(in_block)

    depth_samples = params["WARP_DEPTH_MS"] * 0.001 * SAMPLE_RATE
    max_delay = MAX_DELAY_SAMPLES - 2

    for n in range(frames):
        delay_buffer[delay_index, :] = in_block[n, :]

        lfo = 0.5 * (1.0 + np.sin(2.0 * np.pi * lfo_phase))
        lfo_phase += params["WARP_RATE"] / SAMPLE_RATE
        if lfo_phase >= 1.0:
            lfo_phase -= 1.0

        # delay in [0, depth_samples]
        delay_samp = np.clip(depth_samples * lfo, 0.0, max_delay)

        read_pos = (delay_index - delay_samp) % MAX_DELAY_SAMPLES
        i0 = int(np.floor(read_pos)) % MAX_DELAY_SAMPLES
        i1 = (i0 + 1) % MAX_DELAY_SAMPLES
        frac = read_pos - np.floor(read_pos)

        out[n, :] = (1.0 - frac) * delay_buffer[i0, :] + frac * delay_buffer[i1, :]
        delay_index = (delay_index + 1) % MAX_DELAY_SAMPLES

    mix = 0.3
    return (mix * out + (1.0 - mix) * in_block).astype(np.float32)

# =========================
# Audio callback (duplex)
# =========================
def audio_callback(indata, outdata, frames, time, status):
    # Log, but don't abort just because of occasional under/overflows
    if status:
        print(f"Status: {status}", flush=True)

    # Always write something to output (prevents output underflow)
    noise = generate_vinyl_noise(frames)
    outdata[:] = noise

    if indata is None or indata.size == 0:
        return

    try:
        x = indata.astype(np.float32)
        if x.ndim == 1:
            x = x[:, None]
        if x.shape[1] == 1:
            x = np.repeat(x, CHANNELS, axis=1)
        elif x.shape[1] > CHANNELS:
            x = x[:, :CHANNELS]
        elif x.shape[1] < CHANNELS:
            x = np.pad(x, ((0, 0), (0, CHANNELS - x.shape[1])), mode="edge")

        warped = apply_warped_vinyl(x)
        y = warped + params["VINYL_MIX"] * noise

        peak = float(np.max(np.abs(y))) if y.size else 0.0
        if peak > 0.99:
            y *= 0.99 / peak

        outdata[:] = y
    except Exception as e:
        print(f"Callback error: {e}", flush=True)
        outdata.fill(0)

# =========================
# Device selection
# =========================
INPUT_NAME_SUBSTR = "CABLE Output (VB-Audio Virtual "
OUTPUT_NAME_SUBSTRS = ("Speakers", "Headphones")

def find_devices():
    devices = sd.query_devices()

    in_dev = None
    out_dev = None

    for dev in devices:
        name = dev.get("name", "")
        idx = dev.get("index", None)
        if idx is None:
            continue

        if in_dev is None and INPUT_NAME_SUBSTR in name:
            in_dev = idx

        if out_dev is None and any(s in name for s in OUTPUT_NAME_SUBSTRS):
            out_dev = idx

        if in_dev is not None and out_dev is not None:
            break

    # fallback to default output if speakers/headphones not found
    if out_dev is None:
        try:
            out_dev = sd.default.device[1]  # (in, out)
        except Exception:
            out_dev = None

    return in_dev, out_dev

# =========================
# Audio control helpers
# =========================
def _audio_thread():
    global stream, running

    in_dev, out_dev = find_devices()
    if in_dev is None:
        print(f'Could not find input device containing: "{INPUT_NAME_SUBSTR}"')
        running = False
        return
    if out_dev is None:
        print('Could not find output device ("Speakers" or "Headphones") and no default output available.')
        running = False
        return

    print(f"Input:  {sd.query_devices(in_dev)['name']}")
    print(f"Output: {sd.query_devices(out_dev)['name']}")

    try:
        stream = sd.Stream(
            device=(in_dev, out_dev),
            samplerate=SAMPLE_RATE,
            blocksize=BLOCK_SIZE,
            channels=CHANNELS,
            dtype="float32",
            callback=audio_callback,
            latency=LATENCY,
        )

        with stream:
            print("Vinyl effect running.")
            while running:
                sd.sleep(100)

    except Exception as e:
        print(f"Audio start failed: {e}", flush=True)
    finally:
        try:
            if stream is not None:
                stream.close()
        except Exception:
            pass
        stream = None
        running = False
        print("Audio stopped.")

def start_audio(sender=None, app_data=None, user_data=None):
    global running, audio_thread_handle
    if running:
        return
    running = True
    audio_thread_handle = threading.Thread(target=_audio_thread, daemon=True)
    audio_thread_handle.start()

def stop_audio(sender=None, app_data=None, user_data=None):
    global running
    running = False

# =========================
# UI
# =========================
def make_slider(name, label, min_v, max_v):
    def cb(sender, app_data, user_data):
        params[user_data] = float(app_data)

    dpg.add_slider_float(
        label=label,
        default_value=float(params[name]),
        min_value=float(min_v),
        max_value=float(max_v),
        callback=cb,
        user_data=name,
        format="%.4f",
        clamped=True,
    )

def main_ui():
    dpg.create_context()
    dpg.create_viewport(title="Vinyl Scratch Controls (Fixed)", width=540, height=640)

    with dpg.window(label="Vinyl Scratch", width=520, height=620):
        dpg.add_text("Noise / crackle")
        make_slider("NOISE_FLOOR", "Noise floor", 0.0, 0.05)
        make_slider("CRACKLE_LEVEL", "Crackle level", 0.0, 0.1)
        make_slider("MICRO_CRACKLE_RATE", "Micro crackle rate", 0.0, 50.0)
        make_slider("POP_RATE", "Pop rate", 0.0, 5.0)
        make_slider("BURST_RATE", "Burst rate", 0.0, 1.0)
        make_slider("BURST_MEAN_LEN", "Burst mean length (s)", 0.05, 1.5)
        make_slider("POP_MIN_AMP", "Pop min amp", 0.0, 1.0)
        make_slider("POP_MAX_AMP", "Pop max amp", 0.0, 1.0)
        make_slider("POP_DECAY", "Pop decay (s)", 0.001, 0.1)

        dpg.add_separator()
        dpg.add_text("Rumble")
        make_slider("RUMBLE_FREQ", "Rumble freq (Hz)", 10.0, 80.0)
        make_slider("RUMBLE_LEVEL", "Rumble level", 0.0, 0.05)

        dpg.add_separator()
        dpg.add_text("Warp / wow")
        make_slider("WARP_RATE", "Warp rate (Hz)", 0.1, 5.0)
        make_slider("WARP_DEPTH_MS", "Warp depth (ms)", 0.0, 10.0)

        dpg.add_separator()
        dpg.add_text("Mix")
        make_slider("VINYL_MIX", "Vinyl mix", 0.0, 1.0)

        dpg.add_separator()
        dpg.add_text('Input must contain: "CABLE Output (VB-Audio Virtual "')
        dpg.add_text('Output must contain: "Speakers" or "Headphones" (or default output).')
        dpg.add_button(label="Start", callback=start_audio)
        dpg.add_same_line()
        dpg.add_button(label="Stop", callback=stop_audio)

    dpg.setup_dearpygui()
    dpg.show_viewport()
    dpg.start_dearpygui()
    dpg.destroy_context()

if __name__ == "__main__":
    main_ui()
