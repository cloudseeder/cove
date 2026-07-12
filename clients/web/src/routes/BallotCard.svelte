<!--
  v0.6.0: renders a kind='ballot' entry in the thread feed.

  Shows the question, single-choice options with a live per-option
  tally, remaining-time countdown, and the caller's current selection.
  Voting happens inline: clicking an option posts a kind='vote' entry
  (or re-posts to change your mind — the tally rule takes the latest
  per voter). Once closes_at passes, the card flips to "Closed" and
  the options become read-only.
-->
<script lang="ts">
  import type { VerifiedEntry } from '$lib/cove/client';

  interface Props {
    /** The ballot entry itself. */
    ve: VerifiedEntry;
    /** All kind='vote' entries in the thread pointing at this ballot,
     *  in seq order. The parent walks entries once and hands us the
     *  filtered slice. */
    votes: VerifiedEntry[];
    /** Caller's pubkey — used to figure out their current vote (if any)
     *  and to show "you voted for X" affordance. */
    myPk: string;
    /** Called with (optionIndex) when the user clicks an option. Absent
     *  when the ballot has closed (button becomes inert). */
    onVote?: (optionIndex: number) => Promise<void>;
  }

  let { ve, votes, myPk, onVote }: Props = $props();

  const options = $derived<string[]>(ve.entry.ballot?.options ?? []);
  const closesAt = $derived<string>(ve.entry.ballot?.closes_at ?? '');
  const question = $derived<string>(ve.entry.body);

  /** Tally rule: for each voter, take their highest-seq vote entry
   *  pointing at this ballot; count(votes per option). Ties broken by
   *  the wire order (irrelevant — we're producing a display, not a
   *  ranking). */
  const tally = $derived.by(() => {
    const perVoter = new Map<string, number>();  // pubkey -> option_index
    for (const v of votes) {
      const idx = v.entry.vote?.option_index;
      if (typeof idx !== 'number') continue;
      perVoter.set(v.entry.author, idx);
    }
    const counts = new Array(options.length).fill(0);
    for (const idx of perVoter.values()) {
      if (idx >= 0 && idx < counts.length) counts[idx]++;
    }
    return { counts, total: perVoter.size, perVoter };
  });

  const myVote = $derived<number | null>(
    tally.perVoter.get(myPk) ?? null,
  );

  /** Closed-state derived from closes_at + wall clock. Recomputes on
   *  each render tick — a card that displays "closing in 12s" flips to
   *  "Closed" on the next tick. For MVP we don't set up a timer; a
   *  reload or scroll re-renders anyway. */
  let now = $state(new Date());
  $effect(() => {
    // Refresh once per minute while mounted so "closes in 12min"
    // isn't stale for hours. Precise sub-second countdowns are noise
    // for HOA-scale ballots.
    const id = setInterval(() => (now = new Date()), 60_000);
    return () => clearInterval(id);
  });
  const closesDate = $derived<Date | null>(
    closesAt ? new Date(closesAt) : null,
  );
  const isClosed = $derived(closesDate !== null && now >= closesDate);

  function remainingText(): string {
    if (!closesDate) return '';
    const diffMs = closesDate.getTime() - now.getTime();
    if (diffMs <= 0) return 'closed';
    const mins = Math.round(diffMs / 60_000);
    if (mins < 60) return `closes in ${mins} min`;
    const hrs = Math.round(mins / 60);
    if (hrs < 48) return `closes in ${hrs}h`;
    const days = Math.round(hrs / 24);
    return `closes in ${days}d`;
  }

  let voting = $state(false);
  let voteError = $state<string | null>(null);

  async function clickOption(idx: number) {
    if (isClosed || !onVote || voting) return;
    if (idx === myVote) return;   // no-op re-click
    voting = true;
    voteError = null;
    try {
      await onVote(idx);
    } catch (e) {
      voteError = e instanceof Error ? e.message : String(e);
    } finally {
      voting = false;
    }
  }

  function percent(i: number): number {
    if (tally.total === 0) return 0;
    return Math.round((tally.counts[i] / tally.total) * 100);
  }
</script>

<div class="ballot-card" class:closed={isClosed}>
  <div class="head">
    <span class="badge" aria-hidden="true">🗳</span>
    <strong class="question">{question}</strong>
    <span class="meta">
      {#if isClosed}Closed{:else}{remainingText()}{/if}
    </span>
  </div>

  <ul class="options" role="radiogroup" aria-label={question}>
    {#each options as opt, i (opt)}
      {@const p = percent(i)}
      {@const chosen = myVote === i}
      <li>
        <button type="button"
          class="option"
          class:chosen
          class:disabled={isClosed || !onVote || voting}
          role="radio"
          aria-checked={chosen}
          disabled={isClosed || !onVote || voting}
          onclick={() => clickOption(i)}>
          <span class="fill" style="width: {p}%"></span>
          <span class="label">
            <span class="marker">{chosen ? '●' : '○'}</span>
            <span>{opt}</span>
          </span>
          <span class="count">{tally.counts[i]} · {p}%</span>
        </button>
      </li>
    {/each}
  </ul>

  <div class="tally-line">
    {tally.total} vote{tally.total === 1 ? '' : 's'}
    {#if myVote !== null && !isClosed}
      · you voted <strong>{options[myVote]}</strong>
      <span class="muted">(click another to change)</span>
    {/if}
    {#if isClosed && myVote !== null}
      · you voted <strong>{options[myVote]}</strong>
    {/if}
  </div>

  {#if voteError}
    <p class="failure" role="alert">{voteError}</p>
  {/if}
</div>

<style>
  .ballot-card {
    border: 1px solid var(--border);
    border-radius: 10px;
    padding: 0.7rem 0.9rem;
    background: var(--panel, transparent);
    margin: 0.35rem 0;
    display: flex;
    flex-direction: column;
    gap: 0.55rem;
  }
  .ballot-card.closed { opacity: 0.86; }
  .head {
    display: flex;
    align-items: baseline;
    gap: 0.5rem;
    flex-wrap: wrap;
  }
  .badge { font-size: 1.05rem; }
  .question { font-size: 0.95rem; flex: 1; }
  .meta {
    font-size: 0.72rem;
    color: var(--muted);
    text-transform: uppercase;
    letter-spacing: 0.05em;
    white-space: nowrap;
  }
  .options { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 0.3rem; }
  .option {
    position: relative;
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 0.5rem;
    width: 100%;
    padding: 0.5rem 0.7rem;
    background: transparent;
    color: inherit;
    border: 1px solid var(--border);
    border-radius: 8px;
    cursor: pointer;
    overflow: hidden;
    text-align: left;
    font-size: 0.88rem;
  }
  .option:hover:not(.disabled) { border-color: var(--accent, #d4af37); }
  .option.chosen {
    border-color: var(--accent, #d4af37);
    box-shadow: 0 0 0 1px var(--accent, #d4af37) inset;
  }
  .option.disabled { cursor: default; }
  .option .fill {
    position: absolute;
    left: 0; top: 0; bottom: 0;
    background: rgba(212, 175, 55, 0.15);
    transition: width 200ms ease;
    z-index: 0;
  }
  .option .label, .option .count { position: relative; z-index: 1; }
  .option .label { display: inline-flex; gap: 0.4rem; align-items: center; }
  .option .marker { color: var(--accent, #d4af37); }
  .option .count {
    font-size: 0.76rem;
    color: var(--muted);
    font-variant-numeric: tabular-nums;
    white-space: nowrap;
  }
  .tally-line {
    font-size: 0.78rem;
    color: var(--muted);
  }
  .tally-line .muted { font-style: italic; opacity: 0.85; }
  .failure { color: var(--danger, #c33); font-size: 0.82rem; margin: 0; }
</style>
