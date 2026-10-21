import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { ReactNode } from "react";
import { api } from "../api";
import { formatTime } from "../lib/format";

const BARS = 110;
const RATES = [0.75, 1, 1.25, 1.5, 2];

/**
 * The two variants the operator can listen to.
 *
 * `original` is the bytes as disclosed — never re-encoded, so it is the copy a
 * court would be played. `denoised` is the cleaned derivative the transcript was
 * produced from. They differ audibly, and that difference is the whole point of
 * offering the switch: a reviewer deciding whether a passage is intelligible
 * needs to hear the disclosed article, not just the processed one.
 */
type Variant = "original" | "denoised";

const VARIANT_LABELS: Record<Variant, string> = {
  original: "Original",
  denoised: "Denoised",
};

/** Deterministic bar heights so the same recording always draws the same shape. */
function barHeights(seed: string): number[] {
  let h = 2166136261;
  for (let i = 0; i < seed.length; i += 1) {
    h ^= seed.charCodeAt(i);
    h = Math.imul(h, 16777619);
  }
  const heights: number[] = [];
  for (let i = 0; i < BARS; i += 1) {
    h = Math.imul(h ^ (h >>> 15), 2246822507);
    const noise = ((h >>> 8) & 0xffff) / 0xffff;
    // Speech-like envelope: louder in the middle, quiet at the edges.
    const envelope = Math.sin((i / BARS) * Math.PI) * 0.6 + 0.4;
    heights.push(3 + noise * envelope * 22);
  }
  return heights;
}

export default function PlayerDock({
  recordingId,
  title,
  subtitle,
  jumpTo = null,
  jumpToken = 0,
  onTime,
  actions,
  hasDenoised = false,
  fallbackDuration = null,
}: {
  recordingId: string | null;
  title: string;
  subtitle?: string;
  jumpTo?: number | null;
  jumpToken?: number;
  onTime?: (seconds: number) => void;
  actions?: ReactNode;
  /**
   * Whether the backend holds a denoised derivative for this recording.
   *
   * Comes from `Exhibit.has_denoised`, which mirrors the same column the audio
   * endpoint reads to decide between serving and 404ing. Gating the switch on it
   * means the Denoised button is absent rather than broken for an exhibit whose
   * denoise has not been run — the demo's normal state for a fresh upload.
   */
  hasDenoised?: boolean;
  /** Probed length from the exhibit record, shown until the decoder reports one. */
  fallbackDuration?: number | null;
}) {
  const audioRef = useRef<HTMLAudioElement | null>(null);
  const [playing, setPlaying] = useState(false);
  const [current, setCurrent] = useState(0);
  const [duration, setDuration] = useState(0);
  const [rate, setRate] = useState(1);
  /**
   * Which variant is playing. Owned here rather than by the caller so both
   * Find and Review get the switch without passing anything down.
   *
   * Deliberately not derived from `hasDenoised`: an operator who has chosen
   * Denoised and then selects a passage in an exhibit without a derivative
   * should fall back to Original, not be left asking for a 404. The reset below
   * handles the *recording* change; this handles the capability change.
   */
  const [variant, setVariant] = useState<Variant>("original");

  const canDenoise = hasDenoised;
  const effective: Variant = canDenoise ? variant : "original";

  // Falling back is a *display* correction, not a silent one: the button row
  // drops the Denoised option, so the label can never claim a variant that is
  // not being served.
  useEffect(() => {
    if (!canDenoise && variant !== "original") setVariant("original");
  }, [canDenoise, variant]);

  const heights = useMemo(() => barHeights(recordingId ?? "empty"), [recordingId]);
  const total = duration > 0 ? duration : fallbackDuration ?? 0;
  const progress = total > 0 ? current / total : 0;

  /**
   * Where the listener was when they switched variant, and whether they were
   * playing. Changing `src` re-loads the element, which resets `currentTime` to
   * 0 — so an A/B switch that did not carry the position would force the
   * operator to find the passage again on every flip, which is precisely the
   * task this control exists to make easy.
   */
  const carry = useRef<{ at: number; wasPlaying: boolean } | null>(null);

  useEffect(() => {
    setDuration(0);
    setPlaying(false);
    const resume = carry.current;
    if (!resume) {
      setCurrent(0);
      return;
    }
    // The two variants are the same recording at the same sample rate, so a
    // timestamp means the same instant in both. Show it immediately so the
    // waveform does not flash back to zero, then the `loadedmetadata` handler
    // below performs the actual seek once the new source can accept one.
    setCurrent(resume.at);
  }, [recordingId, effective]);

  const switchVariant = (next: Variant) => {
    if (next === effective) return;
    const element = audioRef.current;
    carry.current = {
      at: element && Number.isFinite(element.currentTime) ? element.currentTime : current,
      wasPlaying: element ? !element.paused : playing,
    };
    setVariant(next);
  };

  // Seek-and-play when a caller selects a passage. `jumpToken` forces a re-seek
  // even when the target second is unchanged (clicking the same hit twice).
  useEffect(() => {
    const element = audioRef.current;
    if (!element || jumpTo === null || jumpTo === undefined) return;
    const apply = () => {
      try {
        element.currentTime = Math.max(0, jumpTo);
        setCurrent(Math.max(0, jumpTo));
      } catch {
        /* metadata not ready */
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

  const toggle = useCallback(() => {
    const element = audioRef.current;
    if (!element) return;
    if (element.paused) {
      element.play().then(
        () => setPlaying(true),
        () => setPlaying(false),
      );
    } else {
      element.pause();
      setPlaying(false);
    }
  }, []);

  const seek = (seconds: number) => {
    const element = audioRef.current;
    if (!element) return;
    const target = Math.min(Math.max(0, seconds), total || seconds);
    element.currentTime = target;
    setCurrent(target);
  };

  const src = recordingId ? api.audioUrl(recordingId, effective) : null;

  /**
   * The variant actually being served, in words.
   *
   * Appended to whatever subtitle the caller passes, rather than replacing it:
   * the caller owns the timecode and speaker, and this owns the one fact the
   * caller cannot know — which file the player is reading. Review used to
   * hardcode the word "original" into its subtitle, which was true only until
   * the switch existed; that string is gone and this is the single source, so
   * the label cannot disagree with `src`.
   */
  const variantNote = `playing ${VARIANT_LABELS[effective].toLowerCase()}`;
  const subline = subtitle ? `${subtitle} · ${variantNote}` : variantNote;


  return (
    <div className="dock">
      <button
        className="dock__play"
        type="button"
        onClick={toggle}
        disabled={!src}
        aria-label={playing ? "Pause" : "Play"}
        title={playing ? "Pause (space)" : "Play (space)"}
      >
        {playing ? "❚❚" : "▶"}
      </button>

      <div className="dock__meta">
        <div className="dock__title" title={title}>
          {title}
        </div>
        <div className="dock__sub mono">{subline}</div>
      </div>

      {/* The A/B switch. Sits next to the title, not in `dock__tools`, because
          the tools row is where the caller's own buttons go and this is a
          property of the audio itself. Absent entirely when the exhibit has no
          denoised derivative, so it can never offer a variant the audio
          endpoint would 404. */}
      {canDenoise && (
        <div className="dock__ab" role="group" aria-label="Audio variant">
          {(Object.keys(VARIANT_LABELS) as Variant[]).map((option) => (
            <button
              key={option}
              type="button"
              className={`dock__abbtn ${effective === option ? "is-on" : ""}`}
              aria-pressed={effective === option}
              onClick={() => switchVariant(option)}
              title={
                option === "original"
                  ? "The disclosed audio, as uploaded — never re-encoded"
                  : "The cleaned derivative the transcript was produced from"
              }
            >
              {VARIANT_LABELS[option]}
            </button>
          ))}
        </div>
      )}

      <div
        className="wave"
        role="slider"
        aria-label="Seek"
        aria-valuemin={0}
        aria-valuemax={Math.round(total)}
        aria-valuenow={Math.round(current)}
        tabIndex={0}
        onClick={(event) => {
          const box = event.currentTarget.getBoundingClientRect();
          seek(((event.clientX - box.left) / box.width) * total);
        }}
        onKeyDown={(event) => {
          if (event.key === "ArrowRight") seek(current + 5);
          if (event.key === "ArrowLeft") seek(current - 5);
        }}
      >
        {heights.map((height, index) => (
          <span
            key={index}
            style={{ height: `${height.toFixed(1)}px` }}
            className={index / BARS < progress ? "is-played" : undefined}
          />
        ))}
      </div>

      <div className="dock__time mono">
        {formatTime(current)} / {total > 0 ? formatTime(total) : "--:--"}
      </div>

      <div className="dock__tools">
        <button className="btn btn--sm" type="button" onClick={() => seek(current - 5)}>
          −5s
        </button>
        <button className="btn btn--sm" type="button" onClick={() => seek(current + 5)}>
          +5s
        </button>
        <select
          className="dock__rate"
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
        {actions}
      </div>

      {src && (
        <audio
          ref={audioRef}
          src={src}
          preload="metadata"
          onLoadedMetadata={(event) => {
            const element = event.currentTarget;
            setDuration(element.duration || 0);
            // Complete the variant handoff: the new source can accept a seek
            // only once it has metadata, so this is where the position captured
            // by `switchVariant` is actually applied. Resuming playback is
            // conditional — flipping the switch while paused should leave it
            // paused, or the operator gets sound they did not ask for.
            const resume = carry.current;
            if (!resume) return;
            carry.current = null;
            try {
              element.currentTime = Math.max(0, Math.min(resume.at, element.duration || resume.at));
              setCurrent(element.currentTime);
            } catch {
              /* nothing to restore */
            }
            if (resume.wasPlaying) {
              element.play().then(
                () => setPlaying(true),
                () => setPlaying(false),
              );
            }
          }}
          onError={() => {
            // A variant can disappear between the listing and the click (an
            // exhibit re-processed, a derivative pruned). Rather than show a
            // dead player with a Denoised button still lit, fall back to the
            // disclosed article and drop back to the variant that always
            // exists.
            if (effective !== "original") {
              carry.current = { at: current, wasPlaying: playing };
              setVariant("original");
            }
            setPlaying(false);
          }}
          onTimeUpdate={(event) => {
            const time = event.currentTarget.currentTime;
            setCurrent(time);
            onTime?.(time);
          }}
          onPlay={() => setPlaying(true)}
          onPause={() => setPlaying(false)}
          onEnded={() => setPlaying(false)}
        />
      )}
    </div>
  );
}
