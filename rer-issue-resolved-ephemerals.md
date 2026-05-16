# Expose `resolved_ephemerals` from `pyrer.SolveResult`

## Summary

`pyrer.SolveResult` doesn't expose the ephemeral packages that the solve
landed on, which makes pyrer not quite drop-in compatible with rez's
`Solver.resolved_ephemerals`. The data appears to exist inside
`rer-resolver` already — it just isn't piped through the PyO3 layer.

## Repro

```python
import pyrer

# Ephemerals constrain the solve correctly (good):
r = pyrer.solve([".foo-1", ".foo-2"], [])
# -> status="failed", ".foo-1 <--!--> .foo-2"  ✅

pkg = pyrer.PackageData("app", "1.0", [".feature-2"], [])
r = pyrer.solve(["app", ".feature-1"], [pkg])
# -> status="failed", ".feature-2 <--!--> .feature-1"  ✅

# But for a successful solve, there's nowhere to read the
# resolved ephemeral ranges back from:
r = pyrer.solve([".foo-1"], [])
# -> status="solved", resolved=[], resolved_packages=[]
# No `resolved_ephemerals` attribute exists on r.
```

```python
>>> [a for a in dir(r) if not a.startswith('_')]
['failure_description', 'num_iterations', 'resolved',
 'resolved_packages', 'solve_time_ms', 'status']
```

vs. rez's `Solver`, which returns `solver.resolved_ephemerals` as a list of
`Requirement` objects representing the final intersected ranges of every
ephemeral that participated in the solve.

## Why this matters

Rez ships ephemerals out to downstream consumers —
`ResolvedContext.resolved_ephemerals`, environment-variable exports, etc.
The CHANGELOG for `0.1.0-rc.1` notes that `PackageScope` already has an
ephemeral kind, so the solver tracks them internally — they just aren't
surfaced on the result.

For context: I wired pyrer into rez behind a `use_rer_solver` config flag
(AcademySoftwareFoundation/rez integration on a downstream branch). The
integration works end-to-end except that `resolved_ephemerals` always
comes back empty, which means rez sees no ephemerals in the resolved
context even when the request included them. Filling them in from the
PyO3 bridge would close that last compatibility gap.

## Proposed shape

```python
class SolveResult:
    ...
    resolved_ephemerals: list[str]  # rez-style requirement strings,
                                    # e.g. [".feature-1.5", ".mode-debug"]
```

Same encoding pattern as `resolved` / `failure_description` — stringified
rez requirements that the caller can re-parse with
`rez.version.Requirement` on the rez side. Empty list when no ephemerals
are involved.

## Alternatives considered

Reconstructing ephemerals on the rez side from the request is wrong: the
published value is supposed to be the **intersected range** across all
participating ephemerals (request + every package's `requires`), not the
raw request. Only the solver has that.

## Pointer

The PyO3 binding entry point is `crates/rer-python/src/lib.rs` (the
`SolveResult` PyClass).
