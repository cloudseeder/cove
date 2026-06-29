/**
 * Chat-mode rendering utilities.
 *
 * Pure functions only — kept testable and side-effect-free so the
 * rendering layer can stay dumb.
 */

/** Initials from a display name. "Kevin Brooks" → "KB", "Madonna" → "MA". */
export function initials(displayName: string): string {
  const parts = displayName.trim().split(/\s+/).filter((s) => s.length > 0);
  if (parts.length === 0) return '?';
  if (parts.length === 1) return parts[0].slice(0, 2).toUpperCase();
  return (parts[0][0] + parts[parts.length - 1][0]).toUpperCase();
}

/**
 * Stable HSL color derived from a pubkey. Same input → same color, so the
 * eye learns to recognize an author by their avatar tint without having
 * to read the hex. Saturation/lightness chosen to read clearly against
 * both the dark panel background and dark message text.
 */
export function authorColor(pubkeyHex: string): string {
  let hash = 0;
  const n = Math.min(pubkeyHex.length, 16);
  for (let i = 0; i < n; i++) {
    hash = ((hash << 5) - hash + pubkeyHex.charCodeAt(i)) | 0;
  }
  const hue = Math.abs(hash) % 360;
  return `hsl(${hue}, 55%, 62%)`;
}

/**
 * Display timestamp for chat mode. Shape matches what users have learned
 * from messaging apps:
 *   - <1 minute   → "just now"
 *   - <1 hour     → "12m"
 *   - same day    → "2:34 PM"
 *   - yesterday   → "Yesterday 2:34 PM"
 *   - this year   → "Jun 28"
 *   - earlier     → "Jun 28, 2025"
 *
 * `now` is injectable so tests don't depend on wall time.
 */
export function smartTimestamp(iso: string, now: Date = new Date()): string {
  const t = new Date(iso);
  const diffMs = now.getTime() - t.getTime();
  const diffMin = Math.floor(diffMs / 60_000);
  if (diffMin < 1) return 'just now';
  if (diffMin < 60) return `${diffMin}m`;

  const sameDay = t.toDateString() === now.toDateString();
  const yesterday = new Date(now);
  yesterday.setDate(yesterday.getDate() - 1);
  const isYesterday = t.toDateString() === yesterday.toDateString();
  const time = t.toLocaleTimeString(undefined, { hour: 'numeric', minute: '2-digit' });

  if (sameDay) return time;
  if (isYesterday) return `Yesterday ${time}`;
  if (t.getFullYear() === now.getFullYear()) {
    return t.toLocaleDateString(undefined, { month: 'short', day: 'numeric' });
  }
  return t.toLocaleDateString(undefined, { month: 'short', day: 'numeric', year: 'numeric' });
}

/**
 * Should `curr` be visually grouped under the same author header as
 * `prev`? Two entries group when they share an author AND are within
 * `gapMs` of each other. The first entry of a thread never groups
 * (prev === null).
 */
export function shouldGroupWithPrevious(
  prev: { author: string; created_at: string } | null,
  curr: { author: string; created_at: string },
  gapMs: number = 5 * 60 * 1000,
): boolean {
  if (prev === null) return false;
  if (prev.author !== curr.author) return false;
  const prevTime = new Date(prev.created_at).getTime();
  const currTime = new Date(curr.created_at).getTime();
  return currTime - prevTime <= gapMs;
}
