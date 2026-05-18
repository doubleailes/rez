# Document & name a raw-string `PackageData` fast path

## Summary

`PackageData(name, version, requires=None, variants=None)` already does the
fast thing — PyO3 extracts `requires`/`variants` straight into
`Vec<String>` / `Vec<Vec<String>>`, no per-element `.str()` round-trip,
no `Requirement` construction. Anyone calling the constructor with raw
strings is on the fast path today.

The discoverability is the problem. The docs and the only documented
convenience (`from_rez`) push integrators toward going through
`rez.Package`'s late-bound wrapper, which materialises full
`rez.version.Requirement` objects so that `from_rez` can immediately
stringify them again. That's the slow path, and the wider rez community
will keep landing on it because it's the obvious one.

Proposal: name and document the fast path so callers reach for it.

## The fast path that already exists

```python
data = pkg.data            # raw rez schema dict
pyrer.PackageData(
    data["name"],          # str
    str(data["version"]),  # str
    data.get("requires") or [],            # list[str], already raw
    data.get("variants") or [],            # list[list[str]], already raw
)
```

On a rez `Package`, `pkg.data["requires"]` and `pkg.data["variants"]`
are stored as raw strings (`"python-3+"`, `"!boost"`, etc.), not
`Requirement` instances. PyO3's `FromPyObject for Vec<String>` extracts
those directly into Rust with no conversion overhead.

What `from_rez` does today, by contrast: reads `pkg.requires` (which
goes through rez's `AttributeForwardMeta` + `_wrap_forwarded` and
returns `list[Requirement]`), then calls `str(req)` per element to feed
the constructor — exactly the work the raw path skips.

So the constructor IS the fast path. There's just no obvious sign that
says so.

## What I'd ask for

Pick one or both, in order of cheapness:

### A — call it out in `from_rez`'s docstring

A line in the existing `from_rez` docstring that says: *"For
performance-sensitive callers with raw string requirements available
(e.g. via `pkg.data` on a rez `Package`), use the constructor directly:
`PackageData(name, version, requires_strs, variants_strs)`."*

Zero code change, but stops downstream integrations from leaving perf on
the floor.

### B — a named alias for discoverability

```python
PackageData.from_strings(name, version, requires=(), variants=()) -> PackageData
```

Same body as the current `__new__` (or just a thin wrapper). The name
makes the contract explicit, mirrors `from_rez`, and gives type
checkers / IDEs something to point integrators at. Pure surface-area
addition, no perf change.

## What I'd NOT do (corrected from an earlier draft)

I previously suggested making `from_rez` itself read `pkg.data` to skip
the wrapper. That would couple pyrer to rez's internal data shape (the
`pkg.data` dict structure) without measurable benefit over the existing
constructor. Better to keep `from_rez` as the duck-typed convenience and
let callers opt into the raw constructor when they have raw strings on
hand.

## Why bother filing at all

The first thing a rez integrator does is grep for "rez" in the pyrer
docs, finds `from_rez`, and writes `from_rez(pkg)` in a hot loop. That's
what I did. The Python solver-vs-rer benchmark I ran (188 cases,
3 iterations, Intel Xeon @ 2.80GHz) showed ~7.6× mean speedup against
rer's ~19.5× standalone claim — and a measurable chunk of that gap is
the `Requirement` round-trip that `from_rez` triggers. The constructor
is right there, but it doesn't broadcast that fact.

Whether this lands as a docstring tweak or a `from_strings` alias, the
goal is the same: get future integrators onto the fast path by default.

## Context

Filed from a downstream rez branch wiring pyrer behind a
`use_rer_solver` config flag. With pyrer 0.1.0-rc.8 the shim already
benefits from `resolved_ephemerals` and `load_family`. Switching the
shim's call from `PackageData.from_rez(pkg)` to
`PackageData(pkg.data["name"], str(pkg.data["version"]),
pkg.data.get("requires") or [], pkg.data.get("variants") or [])` was a
local, no-upstream-change win. This issue is purely about making that
win the default for everyone else.
