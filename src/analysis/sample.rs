//! Pitch, length and level of an audio file, for tuning a sample to the set.
//!
//! WAV and AIFF are parsed by hand rather than through a crate: `hound` covers
//! WAV only, AIFF would need hand-rolling regardless, and both formats are a
//! chunk walk plus one shared sample decoder.

use serde_json::{Value, json};

use super::PITCH_NAMES;

/// Analysis windows in seconds. A drum hit's attack and body often differ in
/// pitch, so each is reported rather than one average.
pub const WINDOWS: [(f64, f64); 4] = [(0.03, 0.1), (0.1, 0.2), (0.2, 0.35), (0.35, 0.6)];

const MIN_HZ: f64 = 25.0;
const MAX_HZ: f64 = 2000.0;
/// YIN's dip threshold on the cumulative-mean-normalised difference.
const YIN_THRESHOLD: f64 = 0.15;
/// Above this the best dip is noise rather than a period.
const YIN_UNVOICED: f64 = 0.5;
/// Windows quieter than this (dBFS RMS) are not worth a pitch estimate.
const SILENCE_DB: f64 = -60.0;

#[derive(Debug, Clone)]
pub struct Audio {
    pub format: &'static str,
    pub sample_rate: u32,
    pub channels: u16,
    pub bits: u16,
    pub float: bool,
    pub frames: usize,
    /// Mono mix, used for pitch.
    pub mono: Vec<f32>,
    pub peak: f32,
    pub rms: f32,
}

pub fn decode(bytes: &[u8]) -> Result<Audio, String> {
    if bytes.len() < 12 {
        return Err("file is too short to be WAV or AIFF".into());
    }
    match (&bytes[0..4], &bytes[8..12]) {
        (b"RIFF", b"WAVE") => decode_wav(bytes),
        (b"FORM", b"AIFF") => decode_aiff(bytes, false),
        (b"FORM", b"AIFC") => decode_aiff(bytes, true),
        _ => Err("not a WAV or AIFF file (only those formats are supported)".into()),
    }
}

/// Iterate `(id, body)` chunks after the 12-byte header. Chunks are padded to
/// an even length in both formats.
fn chunks(bytes: &[u8], big_endian: bool) -> impl Iterator<Item = (&[u8], &[u8])> {
    let mut at = 12;
    std::iter::from_fn(move || {
        if at + 8 > bytes.len() {
            return None;
        }
        let id = &bytes[at..at + 4];
        let raw: [u8; 4] = bytes[at + 4..at + 8].try_into().ok()?;
        let size = if big_endian { u32::from_be_bytes(raw) } else { u32::from_le_bytes(raw) } as usize;
        let start = at + 8;
        let end = start.saturating_add(size).min(bytes.len());
        at = start.saturating_add(size).saturating_add(size & 1);
        Some((id, &bytes[start..end]))
    })
}

fn u16le(b: &[u8], at: usize) -> u16 {
    u16::from_le_bytes([b[at], b[at + 1]])
}

fn u32le(b: &[u8], at: usize) -> u32 {
    u32::from_le_bytes([b[at], b[at + 1], b[at + 2], b[at + 3]])
}

fn decode_wav(bytes: &[u8]) -> Result<Audio, String> {
    let mut fmt = None;
    let mut data = None;
    for (id, body) in chunks(bytes, false) {
        match id {
            b"fmt " if body.len() >= 16 => fmt = Some(body),
            b"data" => data = Some(body),
            _ => {}
        }
    }
    let fmt = fmt.ok_or("WAV has no fmt chunk")?;
    let data = data.ok_or("WAV has no data chunk")?;
    let mut tag = u16le(fmt, 0);
    let channels = u16le(fmt, 2);
    let sample_rate = u32le(fmt, 4);
    let block_align = u16le(fmt, 12);
    let bits = u16le(fmt, 14);
    // WAVE_FORMAT_EXTENSIBLE: the real format is the subformat GUID's first two bytes.
    if tag == 0xFFFE && fmt.len() >= 26 {
        tag = u16le(fmt, 24);
    }
    let float = match tag {
        1 => false,
        3 => true,
        other => return Err(format!("unsupported WAV encoding (format tag {other:#x})")),
    };
    if channels == 0 {
        return Err("WAV declares zero channels".into());
    }
    let container = (block_align / channels) as usize;
    let encoding = match (float, container) {
        (false, 1) => Encoding::U8,
        (false, 2..=4) => Encoding::Int { bytes: container, big: false },
        (true, 4) => Encoding::F32 { big: false },
        (true, 8) => Encoding::F64 { big: false },
        _ => return Err(format!("unsupported WAV sample size: {bits} bits")),
    };
    build("wav", sample_rate, channels, bits, float, data, encoding)
}

fn decode_aiff(bytes: &[u8], compressed: bool) -> Result<Audio, String> {
    let mut comm = None;
    let mut ssnd = None;
    for (id, body) in chunks(bytes, true) {
        match id {
            b"COMM" if body.len() >= 18 => comm = Some(body),
            b"SSND" if body.len() >= 8 => ssnd = Some(body),
            _ => {}
        }
    }
    let comm = comm.ok_or("AIFF has no COMM chunk")?;
    let ssnd = ssnd.ok_or("AIFF has no SSND chunk")?;
    let channels = u16::from_be_bytes([comm[0], comm[1]]);
    let bits = u16::from_be_bytes([comm[6], comm[7]]);
    let sample_rate = extended_to_f64(&comm[8..18]).round() as u32;
    let compression: &[u8] = if compressed && comm.len() >= 22 { &comm[18..22] } else { b"NONE" };
    let container = (bits as usize).div_ceil(8);
    let encoding = match compression {
        b"NONE" | b"twos" => match container {
            1 => Encoding::I8,
            2..=4 => Encoding::Int { bytes: container, big: true },
            _ => return Err(format!("unsupported AIFF sample size: {bits} bits")),
        },
        b"sowt" => Encoding::Int { bytes: container, big: false },
        b"fl32" | b"FL32" => Encoding::F32 { big: true },
        b"fl64" | b"FL64" => Encoding::F64 { big: true },
        other => {
            return Err(format!(
                "unsupported AIFF-C compression {:?}",
                String::from_utf8_lossy(other)
            ));
        }
    };
    let float = matches!(encoding, Encoding::F32 { .. } | Encoding::F64 { .. });
    let offset = u32::from_be_bytes([ssnd[0], ssnd[1], ssnd[2], ssnd[3]]) as usize;
    let data = ssnd.get(8 + offset..).unwrap_or(&[]);
    build(if compressed { "aifc" } else { "aiff" }, sample_rate, channels, bits, float, data, encoding)
}

/// IEEE 754 80-bit extended, which is how AIFF stores its sample rate.
fn extended_to_f64(b: &[u8]) -> f64 {
    let exponent = (((b[0] & 0x7f) as i32) << 8) | b[1] as i32;
    let mantissa = u64::from_be_bytes(b[2..10].try_into().expect("8 bytes"));
    if exponent == 0 && mantissa == 0 {
        return 0.0;
    }
    let value = mantissa as f64 * 2f64.powi(exponent - 16383 - 63);
    if b[0] & 0x80 != 0 { -value } else { value }
}

#[derive(Clone, Copy)]
enum Encoding {
    U8,
    I8,
    /// Signed integer, left-justified in `bytes`, so dividing by the container's
    /// full scale is right for 24-in-32 too.
    Int { bytes: usize, big: bool },
    F32 { big: bool },
    F64 { big: bool },
}

impl Encoding {
    fn width(self) -> usize {
        match self {
            Encoding::U8 | Encoding::I8 => 1,
            Encoding::Int { bytes, .. } => bytes,
            Encoding::F32 { .. } => 4,
            Encoding::F64 { .. } => 8,
        }
    }

    fn read(self, b: &[u8]) -> f32 {
        match self {
            Encoding::U8 => (b[0] as f32 - 128.0) / 128.0,
            Encoding::I8 => b[0] as i8 as f32 / 128.0,
            Encoding::Int { bytes, big } => {
                let mut word = [0u8; 4];
                // Place the bytes at the top of an i32 so the sign extends.
                for i in 0..bytes {
                    let byte = if big { b[i] } else { b[bytes - 1 - i] };
                    word[i] = byte;
                }
                i32::from_be_bytes(word) as f32 / 2147483648.0
            }
            Encoding::F32 { big } => {
                let raw: [u8; 4] = b[..4].try_into().expect("4 bytes");
                if big { f32::from_be_bytes(raw) } else { f32::from_le_bytes(raw) }
            }
            Encoding::F64 { big } => {
                let raw: [u8; 8] = b[..8].try_into().expect("8 bytes");
                (if big { f64::from_be_bytes(raw) } else { f64::from_le_bytes(raw) }) as f32
            }
        }
    }
}

fn build(
    format: &'static str,
    sample_rate: u32,
    channels: u16,
    bits: u16,
    float: bool,
    data: &[u8],
    encoding: Encoding,
) -> Result<Audio, String> {
    if sample_rate == 0 {
        return Err("file declares a sample rate of 0".into());
    }
    if channels == 0 {
        return Err("file declares zero channels".into());
    }
    let width = encoding.width();
    let frame = width * channels as usize;
    let frames = data.len() / frame;
    let mut mono = Vec::with_capacity(frames);
    let mut peak = 0f32;
    let mut sum_sq = 0f64;
    for f in 0..frames {
        let mut mix = 0f32;
        for c in 0..channels as usize {
            let at = f * frame + c * width;
            let s = encoding.read(&data[at..at + width]);
            peak = peak.max(s.abs());
            sum_sq += (s as f64) * (s as f64);
            mix += s;
        }
        mono.push(mix / channels as f32);
    }
    let total = (frames * channels as usize).max(1) as f64;
    Ok(Audio {
        format,
        sample_rate,
        channels,
        bits,
        float,
        frames,
        mono,
        peak,
        rms: (sum_sq / total).sqrt() as f32,
    })
}

fn db(x: f64) -> Option<f64> {
    (x > 0.0).then(|| (20.0 * x.log10() * 10.0).round() / 10.0)
}

/// MIDI number, possibly fractional. A4 = 440 Hz = 69.
pub fn midi_of(hz: f64) -> f64 {
    69.0 + 12.0 * (hz / 440.0).log2()
}

/// Scientific pitch name: MIDI 60 is C4. Live's piano roll calls it C3.
pub fn note_name(midi: i32) -> String {
    format!("{}{}", PITCH_NAMES[midi.rem_euclid(12) as usize], midi.div_euclid(12) - 1)
}

#[derive(Debug, Clone, Copy)]
pub struct Pitch {
    pub hz: f64,
    /// 1 minus YIN's normalised difference at the chosen period.
    pub confidence: f64,
}

/// YIN over `x`: the lag whose cumulative-mean-normalised difference first dips
/// under the threshold, refined by parabolic interpolation. Taking the first
/// dip rather than the deepest is what keeps it off sub-octaves, and the
/// difference function averages over the whole window, so broadband noise on a
/// hit does not create the spurious crossings that fool zero-crossing counts.
pub fn yin(x: &[f32], sample_rate: u32) -> Option<Pitch> {
    let sr = sample_rate as f64;
    let min_lag = (sr / MAX_HZ).floor().max(2.0) as usize;
    let max_lag = ((sr / MIN_HZ).ceil() as usize).min(x.len() / 2);
    if max_lag <= min_lag + 2 {
        return None;
    }
    // d(τ) averaged over the overlap, so long lags are not penalised for
    // summing fewer terms.
    let mut d = vec![0f64; max_lag + 2];
    for (tau, slot) in d.iter_mut().enumerate().skip(1).take(max_lag + 1) {
        let n = x.len() - tau;
        let mut sum = 0f64;
        for j in 0..n {
            let diff = (x[j] - x[j + tau]) as f64;
            sum += diff * diff;
        }
        *slot = sum / n as f64;
    }
    let mut cmnd = vec![1f64; max_lag + 2];
    let mut running = 0f64;
    for tau in 1..=max_lag + 1 {
        running += d[tau];
        cmnd[tau] = if running > 0.0 { d[tau] * tau as f64 / running } else { 1.0 };
    }

    let mut chosen = None;
    let mut tau = min_lag;
    while tau <= max_lag {
        if cmnd[tau] < YIN_THRESHOLD {
            // The minimum of the whole dip, not the first local minimum: noise
            // makes the curve jagged sample to sample near the true period.
            let mut best = tau;
            while tau < max_lag && cmnd[tau + 1] < YIN_THRESHOLD {
                tau += 1;
                if cmnd[tau] < cmnd[best] {
                    best = tau;
                }
            }
            chosen = Some(best);
            break;
        }
        tau += 1;
    }
    let tau = match chosen {
        Some(t) => t,
        None => {
            let best = (min_lag..=max_lag).min_by(|&a, &b| cmnd[a].total_cmp(&cmnd[b]))?;
            if cmnd[best] > YIN_UNVOICED {
                return None;
            }
            best
        }
    };

    let (a, b, c) = (cmnd[tau - 1], cmnd[tau], cmnd[tau + 1]);
    let denom = a - 2.0 * b + c;
    let shift = if denom.abs() > 1e-12 { (0.5 * (a - c) / denom).clamp(-1.0, 1.0) } else { 0.0 };
    let period = tau as f64 + shift;
    Some(Pitch { hz: sr / period, confidence: (1.0 - b).clamp(0.0, 1.0) })
}

fn describe_pitch(hz: f64) -> Value {
    let midi = midi_of(hz);
    let nearest = midi.round() as i32;
    json!({
        "hz": (hz * 100.0).round() / 100.0,
        "note": note_name(nearest),
        "cents": ((midi - nearest as f64) * 100.0 * 10.0).round() / 10.0,
        "midi": (midi * 1000.0).round() / 1000.0,
    })
}

/// The Simpler Transpose (semitones) and Detune (cents) that move `hz` onto the
/// nearest root or fifth of the set's key. Detune stays within Simpler's ±50.
pub fn tuning(hz: f64, root_note: u8, scale_name: Option<&str>) -> Value {
    let midi = midi_of(hz);
    let root = (root_note % 12) as i32;
    let fifth = (root + 7) % 12;
    let mut best: Option<(f64, i32, &str)> = None;
    for (pc, degree) in [(root, "root"), (fifth, "fifth")] {
        let base = (midi / 12.0).floor() as i32 * 12 + pc;
        for target in [base - 12, base, base + 12] {
            let shift = target as f64 - midi;
            if best.is_none_or(|(s, _, _)| shift.abs() < s.abs()) {
                best = Some((shift, target, degree));
            }
        }
    }
    let (shift, target, degree) = best.expect("candidates are never empty");
    let transpose = shift.round();
    let detune = ((shift - transpose) * 100.0).round();
    json!({
        "key": format!("{} {}", PITCH_NAMES[root as usize], scale_name.unwrap_or("")).trim().to_string(),
        "target": degree,
        "target_note": note_name(target),
        "target_hz": ((440.0 * 2f64.powf((target as f64 - 69.0) / 12.0)) * 100.0).round() / 100.0,
        "shift_semitones": (shift * 1000.0).round() / 1000.0,
        "transpose": transpose as i64,
        "detune": detune as i64,
        "note": "Absolute Simpler settings, assuming the sample is triggered at its root key \
                 (C3 in Live) with no other pitch modulation.",
    })
}

/// Everything `analyze_sample` reports about a decoded file, except tuning.
pub fn analyze(audio: &Audio) -> (Value, Option<f64>) {
    let sr = audio.sample_rate as f64;
    let mut windows = Vec::new();
    let mut body = Vec::new();
    for (start, end) in WINDOWS {
        let a = (start * sr) as usize;
        let b = ((end * sr) as usize).min(audio.mono.len());
        let label = format!("{start:.2}-{end:.2} s");
        if b <= a + 16 {
            windows.push(json!({ "window": label, "pitch": null, "reason": "past the end of the sample" }));
            continue;
        }
        let x = &audio.mono[a..b];
        let rms = (x.iter().map(|s| (*s as f64).powi(2)).sum::<f64>() / x.len() as f64).sqrt();
        let rms_db = db(rms);
        if rms_db.is_none_or(|v| v < SILENCE_DB) {
            windows.push(json!({ "window": label, "rms_db": rms_db, "pitch": null, "reason": "silent" }));
            continue;
        }
        match yin(x, audio.sample_rate) {
            Some(p) => {
                let mut entry = describe_pitch(p.hz);
                entry["window"] = json!(label);
                entry["confidence"] = json!((p.confidence * 100.0).round() / 100.0);
                entry["rms_db"] = json!(rms_db);
                if start >= 0.1 {
                    body.push(p.hz);
                }
                windows.push(entry);
            }
            None => windows.push(json!({
                "window": label, "rms_db": rms_db, "pitch": null, "reason": "no stable period",
            })),
        }
    }
    // The body is what sustains under the mix, so tuning follows it rather than
    // the attack. A short sample with no body windows falls back to the attack.
    if body.is_empty()
        && let Some(hz) = windows.first().and_then(|w| w.get("hz")).and_then(Value::as_f64)
    {
        body.push(hz);
    }
    body.sort_by(f64::total_cmp);
    let body_hz = (!body.is_empty()).then(|| body[body.len() / 2]);

    let report = json!({
        "format": audio.format,
        "sample_rate": audio.sample_rate,
        "channels": audio.channels,
        "bits": audio.bits,
        "float": audio.float,
        "length_seconds": ((audio.frames as f64 / sr) * 1000.0).round() / 1000.0,
        "peak": (audio.peak as f64 * 10000.0).round() / 10000.0,
        "peak_db": db(audio.peak as f64),
        "rms_db": db(audio.rms as f64),
        "windows": windows,
        "body_pitch": body_hz.map(describe_pitch),
        "note_naming": "Scientific pitch, A4 = 440 Hz and MIDI 60 = C4; Live's piano roll calls MIDI 60 C3.",
    });
    (report, body_hz)
}

#[cfg(test)]
pub(crate) mod tests {
    use super::*;

    /// A decaying sine with a little deterministic noise, as a kick's body.
    pub fn sine(hz: f64, seconds: f64, sr: u32, noise: f32) -> Vec<f32> {
        let mut seed = 0x2545F491u32;
        (0..(seconds * sr as f64) as usize)
            .map(|i| {
                let t = i as f64 / sr as f64;
                seed = seed.wrapping_mul(1664525).wrapping_add(1013904223);
                let n = (seed >> 8) as f32 / (1u32 << 24) as f32 * 2.0 - 1.0;
                (0.8 * (-t * 3.0).exp() * (std::f64::consts::TAU * hz * t).sin()) as f32 + noise * n
            })
            .collect()
    }

    pub fn wav(samples: &[f32], sr: u32, bits: u16, float: bool) -> Vec<u8> {
        let width = (bits / 8) as usize;
        let mut data = Vec::new();
        for &s in samples {
            match (float, width) {
                (true, 4) => data.extend_from_slice(&s.to_le_bytes()),
                (false, 2) => data.extend_from_slice(&((s * 32767.0) as i16).to_le_bytes()),
                (false, 3) => data.extend_from_slice(&((s * 8388607.0) as i32).to_le_bytes()[..3]),
                (false, 4) => data.extend_from_slice(&((s as f64 * 2147483647.0) as i32).to_le_bytes()),
                _ => unreachable!(),
            }
        }
        let mut out = Vec::new();
        out.extend_from_slice(b"RIFF");
        out.extend_from_slice(&(36 + data.len() as u32).to_le_bytes());
        out.extend_from_slice(b"WAVEfmt ");
        out.extend_from_slice(&16u32.to_le_bytes());
        out.extend_from_slice(&(if float { 3u16 } else { 1 }).to_le_bytes());
        out.extend_from_slice(&1u16.to_le_bytes());
        out.extend_from_slice(&sr.to_le_bytes());
        out.extend_from_slice(&(sr * width as u32).to_le_bytes());
        out.extend_from_slice(&(width as u16).to_le_bytes());
        out.extend_from_slice(&bits.to_le_bytes());
        out.extend_from_slice(b"data");
        out.extend_from_slice(&(data.len() as u32).to_le_bytes());
        out.extend_from_slice(&data);
        out
    }

    fn aiff_16(samples: &[f32], sr: u32) -> Vec<u8> {
        let mut data = vec![0u8; 8];
        for &s in samples {
            data.extend_from_slice(&((s * 32767.0) as i16).to_be_bytes());
        }
        let mut comm = Vec::new();
        comm.extend_from_slice(&1u16.to_be_bytes());
        comm.extend_from_slice(&(samples.len() as u32).to_be_bytes());
        comm.extend_from_slice(&16u16.to_be_bytes());
        // 80-bit extended: 44100 = 1.3458... × 2^15.
        let exponent = 16383 + 15u16;
        let mantissa = (sr as u64) << (63 - 15);
        comm.extend_from_slice(&exponent.to_be_bytes());
        comm.extend_from_slice(&mantissa.to_be_bytes());
        let mut out = Vec::new();
        out.extend_from_slice(b"FORM");
        out.extend_from_slice(&((4 + 8 + comm.len() + 8 + data.len()) as u32).to_be_bytes());
        out.extend_from_slice(b"AIFFCOMM");
        out.extend_from_slice(&(comm.len() as u32).to_be_bytes());
        out.extend_from_slice(&comm);
        out.extend_from_slice(b"SSND");
        out.extend_from_slice(&(data.len() as u32).to_be_bytes());
        out.extend_from_slice(&data);
        out
    }

    fn body(bytes: &[u8]) -> (Value, f64) {
        let audio = decode(bytes).expect("decodes");
        let (report, hz) = analyze(&audio);
        (report, hz.expect("a body pitch"))
    }

    #[test]
    fn a_sine_at_49_5_hz_is_g1_about_18_cents_sharp() {
        let samples = sine(49.5, 0.8, 44100, 0.0);
        let (report, hz) = body(&wav(&samples, 44100, 16, false));
        assert!((hz - 49.5).abs() < 0.1, "{hz}");
        let pitch = &report["body_pitch"];
        assert_eq!(pitch["note"], "G1");
        let cents = pitch["cents"].as_f64().unwrap();
        assert!((cents - 17.6).abs() < 2.0, "{cents}");
        assert_eq!(report["length_seconds"], 0.8);
        assert!(report["peak_db"].as_f64().unwrap() < 0.0);
        for window in report["windows"].as_array().unwrap() {
            assert_eq!(window["note"], "G1", "{window}");
        }
    }

    #[test]
    fn every_supported_encoding_decodes_to_the_same_pitch() {
        let samples = sine(110.0, 0.7, 48000, 0.0);
        for bytes in [
            wav(&samples, 48000, 16, false),
            wav(&samples, 48000, 24, false),
            wav(&samples, 48000, 32, false),
            wav(&samples, 48000, 32, true),
            aiff_16(&samples, 48000),
        ] {
            let audio = decode(&bytes).unwrap();
            assert_eq!(audio.sample_rate, 48000);
            assert!((audio.peak - 0.8).abs() < 0.05, "{}", audio.peak);
            let (_, hz) = body(&bytes);
            assert!((hz - 110.0).abs() < 0.2, "{} gave {hz}", audio.format);
        }
    }

    #[test]
    fn noise_on_the_hit_does_not_move_the_estimate() {
        let samples = sine(49.5, 0.8, 44100, 0.05);
        let (_, hz) = body(&wav(&samples, 44100, 24, false));
        assert!((hz - 49.5).abs() < 0.3, "{hz}");
    }

    #[test]
    fn tuning_lands_on_the_nearest_root_or_fifth() {
        // D minor: G1 +18 ct is closest to A1, two semitones up less 18 cents.
        let t = tuning(49.5, 2, Some("Minor"));
        assert_eq!(t["target"], "fifth");
        assert_eq!(t["target_note"], "A1");
        assert_eq!(t["transpose"], 2);
        assert_eq!(t["detune"], -18);
        assert_eq!(t["key"], "D Minor");
        // In G it is already the root, 18 cents sharp.
        let t = tuning(49.5, 7, None);
        assert_eq!((t["target"].as_str(), t["transpose"].as_i64(), t["detune"].as_i64()),
                   (Some("root"), Some(0), Some(-18)));
    }

    #[test]
    fn rejects_what_it_cannot_read() {
        assert!(decode(b"not audio at all").is_err());
        let mut bytes = wav(&[0.0; 10], 44100, 16, false);
        bytes[20] = 2; // ADPCM
        assert!(decode(&bytes).unwrap_err().contains("unsupported"));
    }
}
