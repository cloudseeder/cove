/**
 * hubs.ts persistence + legacy-migration tests (Phase 2 / v0.4.69).
 *
 * All the helpers no-op when localStorage is undefined, so we polyfill
 * an in-memory Map before each test.
 */
import { beforeEach, describe, expect, test } from 'vitest';
import {
  hubLabel, loadActiveHubUrl, loadHubUrls, loadThreadFor,
  migrateLegacyKeys, removeHubUrl, saveActiveHubUrl, saveHubUrls,
  saveThreadFor,
} from './hubs';

function installMemoryLocalStorage(): Map<string, string> {
  const store = new Map<string, string>();
  const ls = {
    getItem: (k: string) => store.get(k) ?? null,
    setItem: (k: string, v: string) => { store.set(k, v); },
    removeItem: (k: string) => { store.delete(k); },
    clear: () => store.clear(),
    key: (i: number) => Array.from(store.keys())[i] ?? null,
    get length() { return store.size; },
  };
  (globalThis as any).localStorage = ls;
  return store;
}

let store: Map<string, string>;

beforeEach(() => {
  store = installMemoryLocalStorage();
});

describe('hub-list round-trip', () => {
  test('save + load returns the same array', () => {
    saveHubUrls(['https://a.example', 'https://b.example']);
    expect(loadHubUrls()).toEqual(['https://a.example', 'https://b.example']);
  });

  test('save deduplicates and preserves insertion order', () => {
    saveHubUrls([
      'https://a.example',
      'https://b.example',
      'https://a.example', // dup
      'https://c.example',
    ]);
    expect(loadHubUrls()).toEqual([
      'https://a.example',
      'https://b.example',
      'https://c.example',
    ]);
  });

  test('load returns [] when the key is missing or malformed', () => {
    expect(loadHubUrls()).toEqual([]);
    store.set('cove.hubs', 'not-json');
    expect(loadHubUrls()).toEqual([]);
    store.set('cove.hubs', '{"nope": true}');
    expect(loadHubUrls()).toEqual([]);
  });
});

describe('activeHubUrl round-trip', () => {
  test('save + load', () => {
    saveActiveHubUrl('https://a.example');
    expect(loadActiveHubUrl()).toBe('https://a.example');
  });

  test('save(null) clears the key', () => {
    saveActiveHubUrl('https://a.example');
    saveActiveHubUrl(null);
    expect(loadActiveHubUrl()).toBeNull();
  });
});

describe('per-hub thread key round-trip', () => {
  test('save + load are hub-scoped', () => {
    saveThreadFor('https://a.example', 'annual-meeting');
    saveThreadFor('https://b.example', 'general');
    expect(loadThreadFor('https://a.example')).toBe('annual-meeting');
    expect(loadThreadFor('https://b.example')).toBe('general');
  });

  test('the two hubs do not collide', () => {
    saveThreadFor('https://a.example', 'thread-a');
    saveThreadFor('https://b.example', 'thread-b');
    // Bonus check: change one, the other is unaffected.
    saveThreadFor('https://a.example', 'thread-a-2');
    expect(loadThreadFor('https://a.example')).toBe('thread-a-2');
    expect(loadThreadFor('https://b.example')).toBe('thread-b');
  });

  test('save(null) clears the per-hub key', () => {
    saveThreadFor('https://a.example', 'annual-meeting');
    saveThreadFor('https://a.example', null);
    expect(loadThreadFor('https://a.example')).toBeNull();
  });
});

describe('removeHubUrl', () => {
  test('drops the URL from the list + drops per-hub thread memory', () => {
    saveHubUrls(['https://a.example', 'https://b.example']);
    saveThreadFor('https://a.example', 'annual-meeting');
    saveThreadFor('https://b.example', 'general');

    removeHubUrl('https://a.example');

    expect(loadHubUrls()).toEqual(['https://b.example']);
    expect(loadThreadFor('https://a.example')).toBeNull();
    // Other hub's thread memory unaffected.
    expect(loadThreadFor('https://b.example')).toBe('general');
  });
});

describe('legacy migration', () => {
  test('migrates cove.hubUrl + cove.thread on first call', () => {
    store.set('cove.hubUrl', 'https://legacy.example');
    store.set('cove.thread', 'legacy-thread');

    migrateLegacyKeys();

    expect(loadHubUrls()).toEqual(['https://legacy.example']);
    expect(loadActiveHubUrl()).toBe('https://legacy.example');
    expect(loadThreadFor('https://legacy.example')).toBe('legacy-thread');
    // Legacy keys are removed.
    expect(store.get('cove.hubUrl')).toBeUndefined();
    expect(store.get('cove.thread')).toBeUndefined();
  });

  test('migrates cove.hubUrl without cove.thread', () => {
    store.set('cove.hubUrl', 'https://legacy.example');

    migrateLegacyKeys();

    expect(loadHubUrls()).toEqual(['https://legacy.example']);
    expect(loadActiveHubUrl()).toBe('https://legacy.example');
    expect(loadThreadFor('https://legacy.example')).toBeNull();
  });

  test('no-op when there is no legacy key', () => {
    migrateLegacyKeys();
    expect(loadHubUrls()).toEqual([]);
    expect(loadActiveHubUrl()).toBeNull();
  });

  test('idempotent — second call cleans up but does not clobber cove.hubs', () => {
    // First-boot state: multi-hub already populated, legacy still around
    // (edge case: a partial migration from a prior version).
    saveHubUrls(['https://new.example']);
    saveActiveHubUrl('https://new.example');
    store.set('cove.hubUrl', 'https://legacy.example');

    migrateLegacyKeys();

    // cove.hubs stayed as-is; legacy got cleaned up.
    expect(loadHubUrls()).toEqual(['https://new.example']);
    expect(loadActiveHubUrl()).toBe('https://new.example');
    expect(store.get('cove.hubUrl')).toBeUndefined();
  });
});

describe('hubLabel', () => {
  test('returns the hostname for a valid URL', () => {
    expect(hubLabel('https://lwccoa-hub.oap.dev')).toBe('lwccoa-hub.oap.dev');
    expect(hubLabel('http://localhost:8000')).toBe('localhost:8000');
  });

  test('falls back to the raw URL when unparseable', () => {
    expect(hubLabel('not a url')).toBe('not a url');
  });
});
