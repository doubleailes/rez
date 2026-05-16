# Support lazy package discovery via a Python callback

## Summary

`pyrer.solve()` currently takes the complete `list[PackageData]` up front,
so any integration must materialise every package the solve might touch
before the Rust algorithm starts. For integrations against rez — where
each `package.py` is arbitrary Python that's only AST-evaluated on
attribute access — this defeats one of the biggest wins rez's own solver
has on cold caches: lazy package loading.

Proposal: let callers pass a `load_family` callback that pyrer invokes
on demand from Rust when it actually needs to inspect a family.

## Problem

Rez's Python solver evaluates `package.py` files lazily through the
`Package` resource wrapper — `pkg.requires`, `pkg.variants` etc. only
trigger AST evaluation on first access. The solver leverages this: it
can bail on a conflicting request after touching two families, while the
rest of the dep graph stays on disk.

Any pyrer integration today has to BFS over reachable families up front
(starting from the request, following `requires`/`variants` to discover
more families) and build the full `PackageData` list before calling
`pyrer.solve()`. Concretely, in the rez integration on a downstream
branch:

```python
def _collect_packages(self, pyrer):
    out, seen, queue = [], set(), []
    for req in self.package_requests:
        if not req.conflict:
            queue.append(req.name); seen.add(req.name)
    while queue:
        name = queue.pop(0)
        for pkg in iter_packages(name, paths=self.package_paths):
            if self.package_filter is not None and self.package_filter.excludes(pkg):
                continue
            out.append(pyrer.PackageData.from_rez(pkg))   # evaluates pkg.requires / pkg.variants
            for dep_name in _gather_dependency_families(pkg):
                if dep_name not in seen:
                    seen.add(dep_name); queue.append(dep_name)
    return out
```

Every `PackageData.from_rez(pkg)` reads and evaluates a `package.py`,
even for families the solver may never reach because of an early
conflict. On a hot filesystem the cost is small; on cold NFS / network
storage with many families, the eager load can easily exceed the
algorithmic savings pyrer is buying.

## Proposed API

Add an optional callback that pyrer invokes when it first needs a family:

```python
def solve(
    package_requests: list[str],
    packages: list[PackageData] | None = None,    # eager set, still accepted
    *,
    load_family: Callable[[str], list[PackageData]] | None = None,
    variant_select_mode: str = "version_priority",
    ...
) -> SolveResult: ...
```

Semantics:

- If `load_family` is given, pyrer treats `packages` as the *initially
  known* set (may be empty / `None`) and calls `load_family(name)` the
  first time it needs to enumerate versions/variants of a family it
  hasn't seen.
- The callback returns the full `list[PackageData]` for that family
  (zero-or-more `PackageData` instances, all sharing `name`). An empty
  list means "no such family"; pyrer should treat that the same way it
  treats an unknown family today.
- pyrer caches the result internally per solve — `load_family` is called
  at most once per name.
- Calls happen from Rust; pyrer should release the GIL around algorithm
  work and reacquire only for the callback invocation. PyO3's
  `Python::with_gil` makes this straightforward.

Pure-Python users keep the existing all-eager call shape. rez (and any
loader-driven integration) gets to drive discovery lazily.

## Why this is the right place to put the laziness

The shim can't recover the lazy behaviour by being clever about its BFS:

- "Don't expand through conflict-only (`!`) and weak (`~`) edges" only
  helps on the seed pass — once a non-conflict edge into a family
  exists, you have to load it, even if the solve would have rejected
  that family on the first scope it generated.
- "Speculate by request order" doesn't generalise — variant ordering
  and intersect/reduce passes can reach back into families discovered
  late.

Only the solver knows when it actually needs a family. So that's where
the load decision belongs.

## Performance shape

The win shows up most on cold caches and broad dep graphs:

- Cold NFS / packages-on-network-storage: rez's Python solver currently
  beats pyrer on early-bailout requests because of pure I/O savings.
  Lazy loading closes that.
- Requests that fail fast in conflict: the Python solver touches a
  handful of `package.py` files; eager-BFS pyrer can touch dozens.
- Wide healthy resolves where the algorithm exercises lots of
  backtracking — already pyrer's sweet spot — change very little, since
  the eventual load set ends up being most of the graph anyway.

## Context

Filed against the rez integration on a downstream branch wiring pyrer
behind a `use_rer_solver` config flag. With pyrer 0.1.0-rc.7 the
behavioural compatibility is good (ephemerals now surface correctly,
278 / 278 rez selftests pass parameterised through the shim). The
remaining production concern is exactly this eager-load floor, which
no amount of shim work can remove without an API hook on the rer side.

## Pointers

- The entry point is `pyrer.solve` in `crates/rer-python/src/lib.rs`.
- Inside `rer-resolver`, the natural injection point is wherever the
  solver currently looks up a family in the internal package map — a
  `Box<dyn Fn(&str) -> Vec<PackageData>>` (or equivalent) carried on
  the `Solver` and consulted on miss.
- PyO3 reference for calling Python from Rust:
  https://pyo3.rs/v0.22.0/python_from_rust/calling_existing_code
