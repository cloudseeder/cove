import { describe, expect, test } from 'vitest';
import { sanitizeThreadName } from './threadname';

describe('sanitizeThreadName', () => {
  test.each([
    // Already canonical: passes through.
    ['annual-meeting', 'annual-meeting'],
    ['budget-2026', 'budget-2026'],
    // Uppercase → lowercase.
    ['Annual-Meeting', 'annual-meeting'],
    ['ANNUAL-MEETING', 'annual-meeting'],
    // Spaces → hyphens.
    ['annual meeting', 'annual-meeting'],
    ['  Annual   Meeting  ', 'annual-meeting'],
    // Punctuation stripped.
    ['Annual Meeting #1', 'annual-meeting-1'],
    ["Q4 budget!", 'q4-budget'],
    // Runs of hyphens collapse; edge hyphens trimmed.
    ['---annual--meeting---', 'annual-meeting'],
    // Empty / all-symbols → empty (caller checks).
    ['', ''],
    ['!@#$%', ''],
    ['---', ''],
    // Unicode that isn't a-z0-9- gets stripped (no transliteration).
    ['café-talk', 'caf-talk'],
  ])('%j → %j', (input, expected) => {
    expect(sanitizeThreadName(input)).toBe(expected);
  });
});
