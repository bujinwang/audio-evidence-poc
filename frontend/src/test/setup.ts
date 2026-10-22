import "@testing-library/jest-dom/vitest";
import { cleanup } from "@testing-library/react";
import { afterEach, vi } from "vitest";

afterEach(() => {
  cleanup();
});

/**
 * jsdom implements no media playback. `PlayerDock` renders an <audio> element and
 * reads `currentTime`/`duration` from it, so give the prototype just enough for
 * the component to mount without throwing. We are not testing playback here —
 * only that the surrounding result list renders and behaves.
 */
if (!("play" in HTMLMediaElement.prototype)) {
  Object.defineProperty(HTMLMediaElement.prototype, "play", {
    writable: true,
    value: vi.fn().mockResolvedValue(undefined),
  });
}
if (!("pause" in HTMLMediaElement.prototype)) {
  Object.defineProperty(HTMLMediaElement.prototype, "pause", {
    writable: true,
    value: vi.fn(),
  });
}
