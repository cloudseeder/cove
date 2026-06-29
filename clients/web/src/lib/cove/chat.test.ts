import { describe, expect, test } from 'vitest';
import { authorColor, initials, shouldGroupWithPrevious, smartTimestamp } from './chat';

describe('initials', () => {
  test.each([
    ['Kevin Brooks', 'KB'],
    ['Madonna', 'MA'],
    ['Mary Jane Watson', 'MW'],
    ['kevin', 'KE'],
    ['', '?'],
    ['  ', '?'],
    ['  Bill   Murray  ', 'BM'],
  ])('%j → %j', (input, expected) => {
    expect(initials(input)).toBe(expected);
  });
});

describe('authorColor', () => {
  test('stable for same input', () => {
    expect(authorColor('abc123')).toBe(authorColor('abc123'));
  });
  test('differs for different inputs', () => {
    expect(authorColor('abc123')).not.toBe(authorColor('def456'));
  });
  test('returns hsl format', () => {
    expect(authorColor('abc123')).toMatch(/^hsl\(\d+, 55%, 62%\)$/);
  });
});

describe('smartTimestamp', () => {
  const NOW = new Date('2026-06-29T15:00:00Z');

  test('under 1 minute', () => {
    expect(smartTimestamp('2026-06-29T14:59:30Z', NOW)).toBe('just now');
  });
  test('minutes ago', () => {
    expect(smartTimestamp('2026-06-29T14:50:00Z', NOW)).toBe('10m');
    expect(smartTimestamp('2026-06-29T14:01:00Z', NOW)).toBe('59m');
  });
  test('today (>= 1 hour) renders time', () => {
    // Pick a time within a few hours of NOW so the local-time comparison
    // stays same-day regardless of the test host's timezone.
    const out = smartTimestamp('2026-06-29T12:00:00Z', NOW);
    // Time format is locale-dependent; just confirm it contains digits and a colon.
    expect(out).toMatch(/\d:\d{2}/);
    expect(out).not.toMatch(/^Yesterday/);
  });
  test('yesterday prefix', () => {
    const out = smartTimestamp('2026-06-28T12:00:00Z', NOW);
    expect(out).toMatch(/^Yesterday/);
  });
  test('same-year date', () => {
    const out = smartTimestamp('2026-03-15T12:00:00Z', NOW);
    expect(out).toMatch(/Mar/);
    expect(out).not.toMatch(/2026/);
  });
  test('prior year includes year', () => {
    const out = smartTimestamp('2025-03-15T12:00:00Z', NOW);
    expect(out).toMatch(/2025/);
  });
});

describe('shouldGroupWithPrevious', () => {
  const A = (created_at: string, author = 'a') => ({ author, created_at });

  test('null prev → never groups', () => {
    expect(shouldGroupWithPrevious(null, A('2026-06-29T10:00:00Z'))).toBe(false);
  });
  test('same author within 5min → groups', () => {
    expect(shouldGroupWithPrevious(
      A('2026-06-29T10:00:00Z'),
      A('2026-06-29T10:03:00Z'),
    )).toBe(true);
  });
  test('same author over 5min → does not group', () => {
    expect(shouldGroupWithPrevious(
      A('2026-06-29T10:00:00Z'),
      A('2026-06-29T10:06:00Z'),
    )).toBe(false);
  });
  test('different author → does not group', () => {
    expect(shouldGroupWithPrevious(
      A('2026-06-29T10:00:00Z', 'a'),
      A('2026-06-29T10:01:00Z', 'b'),
    )).toBe(false);
  });
  test('custom gap honored', () => {
    expect(shouldGroupWithPrevious(
      A('2026-06-29T10:00:00Z'),
      A('2026-06-29T10:30:00Z'),
      60 * 60 * 1000,
    )).toBe(true);
  });
});
