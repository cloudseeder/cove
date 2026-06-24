# translog implementation notes (VNTP tamper-evident log)

Orientation for building `cove/translog.py` against `tests/test_translog.py`.
Spec: `server-hub-spec.md` §6.4. This is the supervised core (CLAUDE.md) — build it
one piece at a time, make each test green without weakening it, commit per green test.

The construction is **RFC 6962** (Certificate Transparency) Merkle Tree Hash, with the
verification algorithms from **RFC 9162**. Don't invent a tree shape — use this one. It
is history-independent and append-friendly, which is exactly what the consistency proof
relies on.

---

## 1. Hashing — domain separation is mandatory

Two one-byte prefixes keep a leaf from ever being confusable with an internal node
(this is what blocks second-preimage attacks; skipping it is a real vulnerability, not
a style choice).

```python
import hashlib, struct

LEAF_PREFIX = b"\x00"
NODE_PREFIX = b"\x01"

def _sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()

def hash_leaf(entry_id: str, seq: int) -> str:
    # leaf commits to BOTH the entry id and its assigned seq (its position claim)
    data = struct.pack(">Q", seq) + entry_id.encode("ascii")
    return _sha(LEAF_PREFIX + data)

def hash_node(left_hex: str, right_hex: str) -> str:
    return _sha(NODE_PREFIX + bytes.fromhex(left_hex) + bytes.fromhex(right_hex))
```

**Critical separation of concerns (the classic double-hash bug):** `hash_leaf` already
applies the `0x00` leaf hash. Everything below operates on lists of *leaf hashes* —
i.e. the output of `hash_leaf`. `mth` only ever applies `hash_node` (`0x01`). Never run
`hash_leaf` twice, and never feed raw entry ids into `mth`.

Helper — largest power of two strictly less than n:

```python
def lp2(n: int) -> int:           # n >= 2
    return 1 << ((n - 1).bit_length() - 1)
# lp2(2)=1, lp2(3)=2, lp2(4)=2, lp2(5)=4, lp2(8)=4
```

---

## 2. Merkle Tree Hash (the root)

`leaves` is the ordered list of **leaf hashes** (outputs of `hash_leaf`), in seq order.

```python
def mth(leaves: list[str]) -> str:
    n = len(leaves)
    if n == 0:
        return _sha(b"")                 # empty-tree convention
    if n == 1:
        return leaves[0]                 # already a leaf hash; do NOT re-hash
    k = lp2(n)
    return hash_node(mth(leaves[:k]), mth(leaves[k:]))
```

### Worked example you can check by hand (n = 3)

```
leaves = [L0, L1, L2]            # Li = hash_leaf(entry_i, i)
k = lp2(3) = 2
mth([L0,L1,L2]) = hash_node( mth([L0,L1]) , mth([L2]) )
               = hash_node( hash_node(L0,L1) , L2 )
```

If your implementation produces a different root for three leaves, stop — the split point
or the prefixes are wrong.

---

## 3. Inclusion proof (`test_inclusion_proof_*`)

**Generation** — sibling hashes from leaf `m` up to the root, deepest-first:

```python
def audit_path(m: int, leaves: list[str]) -> list[str]:
    n = len(leaves)
    if n <= 1:
        return []
    k = lp2(n)
    if m < k:
        return audit_path(m, leaves[:k]) + [mth(leaves[k:])]
    else:
        return audit_path(m - k, leaves[k:]) + [mth(leaves[:k])]
```

**Verification** — recompute the root from the leaf hash + path. Mirror the generation
recursion exactly (consume the path from the *end*, which is the top-level sibling). This
recursive form is obviously correct and is preferable to the iterative bit-twiddle for the
pilot:

```python
def _recompute_root(leaf_hash: str, m: int, n: int, path: list[str]) -> str:
    if n <= 1:
        return leaf_hash                 # path must be empty here
    k = lp2(n)
    sib, rest = path[-1], path[:-1]
    if m < k:
        return hash_node(_recompute_root(leaf_hash, m, k, rest), sib)
    else:
        return hash_node(sib, _recompute_root(leaf_hash, m - k, n - k, rest))

def verify_inclusion(entry_id, seq, proof, sth) -> bool:
    # proof carries leaf_index (m) and tree_size (n); see InclusionProof fields
    leaf = hash_leaf(entry_id, seq)
    if not (0 <= proof.leaf_index < proof.tree_size):
        return False
    return _recompute_root(leaf, proof.leaf_index, proof.tree_size, proof.audit_path) \
           == sth.root_hash
```

Note `test_inclusion_proof_fails_for_absent_event` expects an *exception* when you ask for
a proof of an entry that isn't in the log — so `inclusion_proof(unknown_id)` should raise
(e.g. `KeyError`/`ValueError`), not return a bogus proof.

### Worked example (prove leaf 2 of 3)

```
audit_path(2, [L0,L1,L2]):  m=2 >= k=2 -> audit_path(0,[L2]) + [mth([L0,L1])] = [ hash_node(L0,L1) ]
verify: m=2,n=3,k=2 -> m>=k -> hash_node(path[-1], recompute(L2,0,1,[])) = hash_node(hash_node(L0,L1), L2)
```
…which equals the root from §2. Good.

---

## 4. Consistency proof (`test_consistency_*`)

Proves the old tree (size `m`) is a prefix of the new tree (size `n`), `m <= n` — i.e. the
log only *grew*; nothing was rewritten, reordered, or deleted.

**Generation** (RFC 6962 SUBPROOF). `b` tracks whether the left subtree's root is one the
verifier already holds (the old root) and can therefore be omitted:

```python
def consistency_proof_path(m: int, leaves: list[str]) -> list[str]:
    n = len(leaves)
    if m == n:
        return []                        # same size: nothing to prove
    return _subproof(m, leaves, True)

def _subproof(m: int, leaves: list[str], b: bool) -> list[str]:
    n = len(leaves)
    if m == n:
        return [] if b else [mth(leaves)]
    k = lp2(n)
    if m <= k:
        return _subproof(m, leaves[:k], b) + [mth(leaves[k:])]
    else:
        return _subproof(m - k, leaves[k:], False) + [mth(leaves[:k])]
```

**Verification** — this is the fiddliest piece in the whole module. The canonical algorithm
is RFC 9162 §2.1.4.2; here it is adapted. **Treat this as the one function to over-test**
(see below) — the bit-shift branches are exactly where a subtle bug hides while the happy
path still passes:

```python
def verify_consistency(proof, old: "STH", new: "STH") -> bool:
    m, n = old.tree_size, new.tree_size
    if not (0 < m <= n):
        raise ValueError("bad sizes")
    if m == n:
        # degenerate case our test exercises: equal size REQUIRES equal root.
        if old.root_hash != new.root_hash:
            raise ValueError("equal size, divergent root -> equivocation")
        return list(proof.path) == []

    p = list(proof.path)
    if (m & (m - 1)) == 0:                # m is a power of two -> old root omitted; prepend it
        p = [old.root_hash] + p
    if not p:
        raise ValueError("empty proof")

    fn, sn = m - 1, n - 1
    while fn & 1:                         # climb to the level of the old tree's right edge
        fn >>= 1; sn >>= 1

    old_r = new_r = p[0]
    for c in p[1:]:
        if sn == 0:
            raise ValueError("proof too long")
        if (fn & 1) or (fn == sn):
            old_r = hash_node(c, old_r)
            new_r = hash_node(c, new_r)
            while (not (fn & 1)) and fn != 0:
                fn >>= 1; sn >>= 1
        else:
            new_r = hash_node(new_r, c)
        fn >>= 1; sn >>= 1

    if sn != 0:
        raise ValueError("proof too short")
    if old_r != old.root_hash:
        raise ValueError("old root mismatch -> history rewritten")
    if new_r != new.root_hash:
        raise ValueError("new root mismatch")
    return True
```

`test_consistency_detects_rewrite` only exercises the **equal-size / divergent-root** branch
(it asks for consistency between two size-4 heads with different roots and expects an
exception). That branch is simple and the code above raises there. **But do not trust the
general algorithm on that test alone** — add cases that hit the growth branches before
relying on it:

```
3 -> 7,  4 -> 7,  6 -> 8,  1 -> 4,  5 -> 8
```
For each, build the new tree, take old = mth(first m leaves), generate the proof, and assert
`verify_consistency` returns True; then corrupt one proof node and assert it raises. Those
five sizes cover the power-of-two, `fn == sn`, and odd/even climb branches.

---

## 5. Signed Tree Head

```python
@dataclass
class STH:
    tree_size: int
    root_hash: str
    prev_sth_hash: str      # sha256 hex of canonical(previous STH incl. its sig); "sha256:000..0" for the first
    timestamp: str          # rfc3339
    hub_key: str            # hub operational PUBLIC key (hex)
    sig: str
```

Signing / verifying uses the existing primitives (`cove.crypto`) and JCS canonicalization,
exactly like entries:

```python
def _sth_content(sth) -> dict:   # everything except sig
    return {k: getattr(sth, k) for k in
            ("tree_size","root_hash","prev_sth_hash","timestamp","hub_key")}

# sign:   sth.sig = crypto.sign(hub_private_hex, crypto.canonicalize(_sth_content(sth)))
# verify: crypto.verify(sth.hub_key, sth.sig, crypto.canonicalize(_sth_content(sth)))
```

**Security note that the test does *not* enforce but production must:** `verify_sth` checking
the signature against `sth.hub_key` only proves "signed by whoever holds that key." A malicious
hub could present a *different* key. The client must separately **pin** the hub's real
operational public key (obtained out-of-band at enrollment, alongside the org root key) and
compare `sth.hub_key` against the pinned value. Verifying the embedded key against itself is
necessary but not sufficient. Note this in the client when you get there.

`prev_sth_hash` chains the heads so the STH history is itself append-only; a verifier walking
heads can confirm each new STH chains onto the one it already trusts.

---

## 6. Storage for the pilot (don't over-engineer)

Source of truth is the entry store (§9). The translog needs only the ordered list of
**leaf hashes**; persist `(seq, entry_id, leaf_hash)` in a `translog_leaves` table and the
STH history in a `sth` table. Recompute `mth` / paths on demand from the leaf-hash list.

At pilot scale this is trivial — a full board-year is a few thousand leaves, and recomputing
a root is microseconds. **Do not** build an incremental/cached tree, a Merkle mountain range,
or persisted internal nodes yet; that's a later optimization once profiling says so. Correct
and recomputed beats clever and wrong, especially here.

`append(entry_id, seq)` = append the leaf hash to the list (and table). `current_sth()` =
`mth(leaves)`, chain `prev_sth_hash`, stamp, sign. That's it.

---

## 7. Build order for this slice (maps to the 5 red tests)

1. `hash_leaf`, `hash_node`, `lp2`, `mth` → check the n=3 worked example by hand.
2. STH build + sign + `verify_sth` → `test_sth_is_signed_by_hub_key`.
3. `audit_path` + `verify_inclusion` (+ raise on absent) → the two `test_inclusion_*`.
4. `consistency_proof_path` (generation) → easy, structural.
5. `verify_consistency` → `test_consistency_detects_rewrite`, **then add the §4 extra-size
   tests yourself** before trusting it.

Commit after each green step. Keep `mth`/`audit_path`/`_subproof` pure and side-effect-free so
they're trivially unit-testable in isolation — that purity is what lets you trust the core the
rest of the hub stands on.

