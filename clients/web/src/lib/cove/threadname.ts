/**
 * Thread-name canonicalization.
 *
 * Cove thread names are lowercase, hyphen-separated, alphanumeric-only.
 * Any input field that creates or addresses a thread should sanitize
 * through this helper before submitting, so "Annual Meeting #1" and
 * "ANNUAL-MEETING-1" both round-trip to the same wire-form
 * "annual-meeting-1".
 */
export function sanitizeThreadName(input: string): string {
  return input
    .toLowerCase()
    .trim()
    .replace(/\s+/g, '-')          // spaces → single hyphen
    .replace(/[^a-z0-9-]/g, '')    // strip everything else
    .replace(/-+/g, '-')           // collapse runs of hyphens
    .replace(/^-+|-+$/g, '');      // trim leading/trailing hyphens
}
