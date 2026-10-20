import { useEffect, useRef, useState } from "react";
import { formatTime } from "../lib/format";

interface AudioPlayerProps {
  src: string | null;
  /** Target position in seconds. Applied when `jumpToken` changes. */
  jumpTo?: number | null;
  jumpToken?: number;
  onTime?: (seconds: number) => void;
  title?: string;
  subtitle?: string;
  compact?: boolean;
  /** Optional alternate source (e.g. the denoised derivative) for A/B listening. */
  altSrc?: string | null;
  altLabel?: string;
  primaryLabel?: string;
}

const RATES = [0.75, 1, 1.25, 1.5, 2];

export default function AudioPlayer({
  src,
  jumpTo,
  jumpToken = 0,
  onTime,
  title,
  subtitle,
  compact = false,
  altSrc = null,
  altLabel = "Denoised",
  primaryLabel = "Original",
}: AudioPlayerProps) {
  const audioRef = useRef<HTMLAudioElement | null>(null);
  const [playing, setPlaying] = useState(false);
  const [current, setCurrent] = useState(0);
  const [duration, setDuration] = useState(0);
  const [rate, setRate] = useState(1);
  const [error, setError] = useState<string | null>(null);
  const [variant, setVariant] = useState<"original" | "denoised">("original");

  // Carries playback position across an A/B swap so the same instant of audio
  // is heard before and after — that is the whole point of the comparison.
  const carry = useRef<{ time: number; playing: boolean } | null>(null);

  const hasAlt = Boolean(altSrc);
  const activeSrc = variant === "denoised" && altSrc ? altSrc : src;

  useEffect(() => {
    setCurrent(0);
    setDuration(0);
    setPlaying(false);
    setError(null);
    setVariant("original");
    carry.current = null;
  }, [src]);

  // Denoised derivative vanished (e.g. re-processing): fall back to original.
  useEffect(() => {
    if (!hasAlt && variant === "denoised") setVariant("original");
  }, [hasAlt, variant]);

  useEffect(() => {
    const element = audioRef.current;
    if (!element || jumpTo === null || jumpTo === undefined) return;
    const target = Math.max(0, jumpTo);

    const apply = () => {
      try {
        element.currentTime = target;
      } catch {
        /* metadata not ready yet */
      }
      element.play().then(
        () => setPlaying(true),
        () => setPlaying(false),
      );
    };

    if (element.readyState >= 1) apply();
    else element.addEventListener("loadedmetadata", apply, { once: true });
  }, [jumpToken, jumpTo]);

  useEffect(() => {
    if (audioRef.current) audioRef.current.playbackRate = rate;
  }, [rate]);

  /** Swap the source but hold the exact position and the play/pause state. */
  const swapTo = (next: "original" | "denoised") => {
    if (next === variant) return;
    const element = audioRef.current;
    carry.current = element
      ? { time: element.currentTime, playing: !element.paused }
      : null;
    setVariant(next);
  };

  const onLoadedMetadata = (element: HTMLAudioElement) => {
    setDuration(element.duration || 0);
    element.playbackRate = rate;
    const pending = carry.current;
    if (!pending) return;
    carry.current = null;
    try {
      element.currentTime = pending.time;
      setCurrent(pending.time);
    } catch {
      /* ignore */
    }
    if (pending.playing) {
      element.play().then(
        () => setPlaying(true),
        () => setPlaying(false),
      );
    }
  };

  const toggle = () => {
    const element = audioRef.current;
    if (!element) return;
    if (element.paused) {
      element.play().then(() => setPlaying(true), () => setPlaying(false));
    } else {
      element.pause();
      setPlaying(false);
    }
  };

  const seek = (value: number) => {
    const element = audioRef.current;
    if (!element) return;
    element.currentTime = value;
    setCurrent(value);
  };

  return (
    <div className={`player ${compact ? "player--compact" : ""}`}>
      {(title || subtitle) && (
        <div className="player__head">
          <div className="player__title" title={title}>
            {title ?? "No recording selected"}
          </div>
          {subtitle && <div className="player__subtitle">{subtitle}</div>}
        </div>
      )}

      {hasAlt && (
        <div className="ab">
          <span className="ab__label">A/B listen</span>
          <div className="ab__switch" role="group" aria-label="Audio variant">
            <button
              type="button"
              className={`ab__btn ${variant === "original" ? "is-active" : ""}`}
              onClick={() => swapTo("original")}
              aria-pressed={variant === "original"}
            >
              {primaryLabel}
            </button>
            <button
              type="button"
              className={`ab__btn ${variant === "denoised" ? "is-active" : ""}`}
              onClick={() => swapTo("denoised")}
              aria-pressed={variant === "denoised"}
            >
              {altLabel}
            </button>
          </div>
          <span className="ab__hint">
            Switching holds the same position, so you hear the same second both ways.
          </span>
        </div>
      )}

      {activeSrc ? (
        <>
          <audio
            ref={audioRef}
            src={activeSrc}
            preload="metadata"
            onLoadedMetadata={(event) => onLoadedMetadata(event.currentTarget)}
            onTimeUpdate={(event) => {
              const time = event.currentTarget.currentTime;
              setCurrent(time);
              onTime?.(time);
            }}
            onPlay={() => setPlaying(true)}
            onPause={() => setPlaying(false)}
            onEnded={() => setPlaying(false)}
            onError={() => setError("This audio could not be loaded.")}
          />

          <div className="player__scrub">
            <span className="player__time">{formatTime(current)}</span>
            <input
              type="range"
              min={0}
              max={duration || 0}
              step={0.05}
              value={Math.min(current, duration || 0)}
              onChange={(event) => seek(Number(event.target.value))}
              aria-label="Seek"
            />
            <span className="player__time player__time--dim">{formatTime(duration)}</span>
          </div>

          <div className="player__controls">
            <button type="button" className="btn btn--primary btn--icon" onClick={toggle}>
              {playing ? "❚❚ Pause" : "▶ Play"}
            </button>
            <button
              type="button"
              className="btn btn--ghost"
              onClick={() => seek(Math.max(0, current - 5))}
            >
              −5s
            </button>
            <button
              type="button"
              className="btn btn--ghost"
              onClick={() => seek(Math.min(duration, current + 5))}
            >
              +5s
            </button>
            <select
              className="select select--tight"
              value={rate}
              onChange={(event) => setRate(Number(event.target.value))}
              aria-label="Playback speed"
            >
              {RATES.map((value) => (
                <option key={value} value={value}>
                  {value}×
                </option>
              ))}
            </select>
          </div>
          {variant === "denoised" && (
            <div className="player__variant-note">
              Playing the denoised derivative. The original remains the reference recording.
            </div>
          )}
          {error && <div className="player__error">{error}</div>}
        </>
      ) : (
        <div className="player__empty">Select a passage to load the original recording.</div>
      )}
    </div>
  );
}
