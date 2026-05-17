# Faster `PackageData` construction by skipping the rez wrapper layer

## Summary

`pyrer.PackageData.from_rez(pkg)` is one of the two hot paths in any rez
integration (the other being the algorithm itself, which is already in
Rust). Today it goes through the duck-typed Python path
(`pkg.name`, `pkg.version`, `pkg.requires`, `pkg.variants`), which on the
rez side forces:

1. An `AttributeForwardMeta` descriptor lookup per attribute.
2. A `_wrap_forwarded` call that may re-evaluate late-bound source code.
3. Construction of full `rez.version.Requirement` objects for every
   `requires` entry and every variant entry.
4. `str(req)` round-tripping each `Requirement` back to a string for
   pyrer to consume — even though rez stored those strings raw in
   `resource.data["requires"]` to begin with.

Proposal: add a direct constructor that takes raw strings (or reads them
off rez's underlying `resource.data` dict) on the Rust side, skipping
the wrapper round-trip.

## Where the time goes

Per package, `PackageData.from_rez` triggers, in order:

```text
pkg.name              # AttributeForwardMeta -> resource.data["name"]
pkg.version           # AttributeForwardMeta -> Version object
                      # pyrer then calls str(version)
pkg.requires          # AttributeForwardMeta -> _wrap_forwarded ->
                      #   late_bound check -> [Requirement(s) ...]
                      # pyrer then calls str(req) for each
pkg.variants          # same chain, nested one level deeper
```

The cost is dominated by (3) and (4): rez wraps each raw requirement
string into a `Requirement` object (which itself parses the string into
a `Version`/`VersionRange` AST), then pyrer immediately turns it back
into a string. For a package with 5 requires and 3 variants of 4 entries
each, that's 17 `Requirement` round-trips per package — pure overhead
relative to the strings that rez already had on disk.

## Benchmark context

On the 188-case rez benchmark (Intel Xeon @ 2.80GHz, pyrer 0.1.0-rc.8,
Python 3.11), the rez-integration measured **~7.6× mean speedup** vs.
rez's Python solver, against the **~19.5×** rer claims standalone. The
delta is overwhelmingly Python-side overhead in the integration, and
`from_rez` is the single biggest line item we can target without
touching rez or PyO3.

(The other big line item is `iter_packages` itself — that one needs
caching, not API redesign, and is out of scope here.)

## Proposed API

Two non-exclusive options.

### Option A — a raw-string fast path

A new constructor that accepts pre-stringified inputs and skips all
attribute resolution and `Requirement` parsing:

```python
pyrer.PackageData.from_strings(
    name: str,
    version: str,
    requires: Iterable[str] = (),
    variants: Iterable[Iterable[str]] = (),
) -> PackageData
```

Callers (the rez shim included) would feed it directly from
`pkg.resource.data`:

```python
data = pkg.resource.data
pyrer.PackageData.from_strings(
    data["name"],
    data["version"],
    data.get("requires") or (),
    data.get("variants") or (),
)
```

`resource.data["requires"]` is already a `list[str]` in the most common
case (filesystem repo, non-late-bound) — no `Requirement` parsing happens
on either side. Late-bound `requires` would still need the wrapper path;
the shim can fall back to `from_rez` for those packages.

### Option B — make `from_rez` itself smarter

Inside the existing `from_rez`, when the input duck-types as a rez
`Package` (i.e. has a `.resource.data` dict), pull the raw strings from
that dict in Rust via PyO3 rather than walking `pkg.requires` /
`pkg.variants`. Falls back to the current path otherwise.

Pros: zero API surface change, transparent win for every existing caller.
Cons: introduces a coupling to rez's internal `resource.data` shape — if
that's a stability concern, Option A is cleaner.

## Why pyrer-side and not shim-side

The shim *could* construct `PackageData(name, version, requires_strs,
variants_strs)` itself, pulling raw values from `pkg.resource.data`, and
skip `from_rez` entirely. That works today and we'll likely do it on the
rez side either way.

The reason for the upstream ask is that **every** rez integration that
wraps pyrer would want this same optimisation, and bundling it into
pyrer (under either name) means downstream callers don't each
reimplement the same `data["requires"] or ()` dance — with the
subtle late-bound edge-cases that get easy to miss.

## Expected impact

Modest but real. Rough back-of-envelope on the rez benchmark dataset:
~50 packages materialised per resolve × ~10–20 `Requirement` round-trips
per package × ~5–10 µs each saved → 2.5–10 ms per resolve. Against a
169 ms mean, that's a few percent. The bigger wins still live in the
integration itself (package-data caching, smaller working set), but
this one is cheap to land and benefits every consumer.

## Context

Filed against a downstream rez branch wiring pyrer behind a
`use_rer_solver` config flag. With pyrer 0.1.0-rc.8 the shim already
benefits from the `resolved_ephemerals` and `load_family` work — this
issue is the third and last pyrer-side line item I've identified after
benchmarking.

## Pointers

- The current Python-side `from_rez` lives in
  `crates/rer-python/src/lib.rs` (the `PackageData` PyClass).
- rez's underlying data dict is available as `pkg.resource.data` on
  any `rez.packages.Package`. The keys are the raw schema keys
  (`"name"`, `"version"`, `"requires"`, `"variants"`, …) and the values
  are either raw strings/lists or `SourceCode` instances for late-bound
  attributes.
- PyO3 reference for fast dict access from Rust:
  https://pyo3.rs/v0.22.0/python_from_rust/calling_existing_code#using-pydict
