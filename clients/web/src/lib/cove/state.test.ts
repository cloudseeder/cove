/**
 * AppState delegation smokes — Phase 1 (v0.4.68).
 *
 * We're not testing the full AppState (that's covered by the manual
 * end-to-end smoke against LWCCOA + Brooks hubs). These tests pin the
 * behavioural invariant that consumer .svelte files depend on: every
 * per-hub `app.xxx` surface delegates through `app.hub`, and returns a
 * safe default when there's no hub.
 *
 * Constructing a real AppState in node touches the vault (IndexedDB not
 * available) — the initial `refreshVaultStatus` call errors out but is
 * caught internally by the constructor's Promise chain. That's fine
 * for our purposes; the delegation logic we're testing doesn't depend
 * on that path.
 */
import { beforeEach, describe, expect, test, vi } from 'vitest';
import { HubConnection } from './hub.svelte';
import { AppState } from './state.svelte';

/** Same in-memory localStorage shim used by hubs.test.ts. AppState boot
 *  calls migrateLegacyKeys() + loadHubUrls(); the shim makes those safe
 *  in the node test environment. */
beforeEach(() => {
  const store = new Map<string, string>();
  (globalThis as any).localStorage = {
    getItem: (k: string) => store.get(k) ?? null,
    setItem: (k: string, v: string) => { store.set(k, v); },
    removeItem: (k: string) => { store.delete(k); },
    clear: () => store.clear(),
    key: (i: number) => Array.from(store.keys())[i] ?? null,
    get length() { return store.size; },
  };
});

describe('AppState delegation (Phase 1)', () => {
  test('with hub === null, per-hub getters return safe defaults', () => {
    const app = new AppState();
    expect(app.hub).toBeNull();
    expect(app.entries).toEqual([]);
    expect(app.threads).toEqual([]);
    expect(app.inboxRows).toEqual([]);
    expect(app.members).toEqual([]);
    expect(app.revoked).toEqual([]);
    expect(app.myAttestation).toBeNull();
    expect(app.manifest).toBeNull();
    expect(app.client).toBeNull();
    expect(app.replyOpen).toBeNull();
    expect(app.thread).toBe('annual-meeting');
    expect(app.view).toBe('messages');
    expect(app.threadStatus.kind).toBe('idle');
    expect(app.authStatus.kind).toBe('unauthenticated');
    expect(app.isBoardMember).toBe(false);
    expect(app.hasCapability('admin')).toBe(false);
    expect(app.newThreadDialog).toBeNull();
  });

  test('after wiring a hub, getters return hub state', () => {
    const app = new AppState();
    const hub = new HubConnection(app);
    hub.thread = 'general';
    hub.entries = [{ entry: { id: 'sha256:aa' }, seq: 0 } as any];
    hub.myAttestation = { display_name: 'Alice', role: 'board' } as any;
    app.hub = hub;

    expect(app.hub).toBe(hub);
    expect(app.thread).toBe('general');
    expect(app.entries.length).toBe(1);
    expect(app.myAttestation?.display_name).toBe('Alice');
    // isBoardMember: board falls back to default caps map → has 'admin'.
    expect(app.isBoardMember).toBe(true);
  });

  test('per-hub method calls delegate through to the hub', () => {
    const app = new AppState();
    const hub = new HubConnection(app);
    const spy = vi.spyOn(hub, 'openReplyPanel');
    app.hub = hub;

    const ve = { entry: { id: 'sha256:bb' } } as any;
    app.openReplyPanel(ve);

    expect(spy).toHaveBeenCalledWith(ve);
    expect(app.replyOpen).toBe(ve);
  });

  test('reset() disposes the hub and clears it', () => {
    const app = new AppState();
    const hub = new HubConnection(app);
    const disposeSpy = vi.spyOn(hub, 'dispose');
    app.hub = hub;

    app.reset();
    expect(disposeSpy).toHaveBeenCalledOnce();
    expect(app.hub).toBeNull();
    expect(app.entries).toEqual([]);
    expect(app.authStatus.kind).toBe('unauthenticated');
  });
});

// ---- Phase 2 multi-hub API smokes -------------------------------------
describe('AppState multi-hub (Phase 2)', () => {
  test('addHub returns the same instance for the same URL (idempotent)', () => {
    const app = new AppState();
    const a1 = app.addHub('https://a.example');
    const a2 = app.addHub('https://a.example');
    expect(a1).toBe(a2);
    expect(app.hubs.size).toBe(1);
  });

  test('addHub for distinct URLs creates distinct instances + persists', () => {
    const app = new AppState();
    const a = app.addHub('https://a.example');
    const b = app.addHub('https://b.example');
    expect(a).not.toBe(b);
    expect(app.hubs.size).toBe(2);
    // Persisted to localStorage — the round-trip cheks that saveHubUrls
    // was called with the URL list.
    expect(localStorage.getItem('cove.hubs')).toBe(
      JSON.stringify(['https://a.example', 'https://b.example']),
    );
  });

  test('switchToHub updates activeHubUrl + persists', () => {
    const app = new AppState();
    app.addHub('https://a.example');
    app.addHub('https://b.example');

    app.switchToHub('https://b.example');
    expect(app.activeHubUrl).toBe('https://b.example');
    expect(localStorage.getItem('cove.activeHubUrl')).toBe('https://b.example');

    app.switchToHub('https://a.example');
    expect(app.activeHubUrl).toBe('https://a.example');
  });

  test('switchToHub is a no-op for an unknown URL', () => {
    const app = new AppState();
    app.addHub('https://a.example');
    app.switchToHub('https://a.example');
    app.switchToHub('https://unknown.example');
    expect(app.activeHubUrl).toBe('https://a.example');
  });

  test('removeHub disposes the hub + falls back to another active', () => {
    const app = new AppState();
    const a = app.addHub('https://a.example');
    const b = app.addHub('https://b.example');
    const disposeA = vi.spyOn(a, 'dispose');
    app.switchToHub('https://a.example');
    expect(app.activeHubUrl).toBe('https://a.example');

    app.removeHub('https://a.example');
    expect(disposeA).toHaveBeenCalledOnce();
    expect(app.hubs.has('https://a.example')).toBe(false);
    // Active fell back to the remaining hub.
    expect(app.activeHubUrl).toBe('https://b.example');
    expect(app.hubs.get('https://b.example')).toBe(b);
  });

  test('removeHub clears activeHubUrl when the last hub is removed', () => {
    const app = new AppState();
    app.addHub('https://a.example');
    app.switchToHub('https://a.example');
    app.removeHub('https://a.example');
    expect(app.activeHubUrl).toBeNull();
    expect(app.hubs.size).toBe(0);
  });

  test('logoutAll disposes every hub + clears livePriv + clears storage', () => {
    const app = new AppState();
    const a = app.addHub('https://a.example');
    const b = app.addHub('https://b.example');
    const disposeA = vi.spyOn(a, 'dispose');
    const disposeB = vi.spyOn(b, 'dispose');
    app.livePriv = 'aa'.repeat(32);

    app.logoutAll();

    expect(disposeA).toHaveBeenCalledOnce();
    expect(disposeB).toHaveBeenCalledOnce();
    expect(app.hubs.size).toBe(0);
    expect(app.activeHubUrl).toBeNull();
    expect(app.livePriv).toBeNull();
    expect(localStorage.getItem('cove.hubs')).toBe('[]');
    expect(localStorage.getItem('cove.activeHubUrl')).toBeNull();
  });

  test('delegators follow activeHubUrl changes', () => {
    const app = new AppState();
    const a = app.addHub('https://a.example');
    const b = app.addHub('https://b.example');
    a.thread = 'in-a';
    b.thread = 'in-b';

    app.switchToHub('https://a.example');
    expect(app.thread).toBe('in-a');

    app.switchToHub('https://b.example');
    expect(app.thread).toBe('in-b');
  });

  test('restores persisted hubs on boot', () => {
    localStorage.setItem('cove.hubs',
      JSON.stringify(['https://a.example', 'https://b.example']));
    localStorage.setItem('cove.activeHubUrl', 'https://b.example');
    localStorage.setItem('cove.thread.https://a.example', 'saved-thread-a');

    const app = new AppState();

    expect(app.hubs.size).toBe(2);
    expect(app.hubs.has('https://a.example')).toBe(true);
    expect(app.hubs.has('https://b.example')).toBe(true);
    expect(app.activeHubUrl).toBe('https://b.example');
    // Per-hub thread rehydrated from localStorage.
    expect(app.hubs.get('https://a.example')?.thread).toBe('saved-thread-a');
    // Placeholder — not yet authenticated.
    expect(app.hubs.get('https://a.example')?.authStatus.kind).toBe('unauthenticated');
  });

  test('runs legacy migration on boot', () => {
    localStorage.setItem('cove.hubUrl', 'https://legacy.example');
    localStorage.setItem('cove.thread', 'legacy-thread');

    const app = new AppState();

    expect(app.hubs.has('https://legacy.example')).toBe(true);
    expect(app.activeHubUrl).toBe('https://legacy.example');
    expect(app.hubs.get('https://legacy.example')?.thread).toBe('legacy-thread');
    // Legacy keys removed.
    expect(localStorage.getItem('cove.hubUrl')).toBeNull();
    expect(localStorage.getItem('cove.thread')).toBeNull();
  });
});
