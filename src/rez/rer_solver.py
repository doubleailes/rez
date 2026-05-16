# SPDX-License-Identifier: Apache-2.0
# Copyright Contributors to the Rez Project


"""
Rust-backed alternative solver, powered by the ``pyrer`` package.

This module provides a drop-in replacement for :class:`rez.solver.Solver`,
used when ``config.use_rer_solver`` is True. It delegates the core resolve
algorithm to the Rust implementation in ``pyrer``
(see https://github.com/doubleailes/rer), while preserving the public
surface that :class:`rez.resolver.Resolver` relies on.

Currently unsupported (the default Python solver should still be used if
any of these matter):

- ``get_graph()`` returns a minimal graph showing only the resolved
  variants; the rich step-by-step graph produced by the Python solver is
  not reconstructed.
- Solver callbacks, custom package orderers, verbose printing, and
  advanced solve statistics are silently ignored.
- Cyclic-dependency detection is left to ``pyrer``; failures are reported
  as plain :attr:`SolverStatus.failed` rather than ``cyclic``.
"""
from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any, Callable

from rez.exceptions import RezSystemError
from rez.package_repository import package_repo_stats
from rez.packages import get_package, iter_packages
from rez.solver import PackageVariant, SolverCallbackReturn, SolverStatus
from rez.vendor.pygraph.classes.digraph import digraph
from rez.version import Requirement

if TYPE_CHECKING:
    from rez.package_filter import PackageFilterBase
    from rez.package_order import PackageOrderList
    from rez.packages import Package
    from rez.resolved_context import ResolvedContext
    from rez.utils.typing import SupportsWrite


def _import_pyrer() -> Any:
    try:
        import pyrer
    except ImportError as e:
        raise RezSystemError(
            "The 'pyrer' package is required when 'use_rer_solver' is "
            "enabled. Install it with 'pip install pyrer' "
            "(see https://github.com/doubleailes/rer)."
        ) from e
    return pyrer


class RerSolver:
    """A :class:`rez.solver.Solver`-compatible front-end that delegates the
    resolve to :func:`pyrer.solve`.

    Only the attributes that :meth:`rez.resolver.Resolver._solver_to_dict`
    reads are guaranteed to be populated; everything else is best-effort.
    """

    max_verbosity = 3

    def __init__(self,
                 package_requests: list[Requirement],
                 package_paths: list[str],
                 context: ResolvedContext | None = None,
                 package_filter: PackageFilterBase | None = None,
                 package_orderers: PackageOrderList | None = None,
                 callback: Callable | None = None,
                 building: bool = False,
                 optimised: bool = True,
                 verbosity: int = 0,
                 buf: SupportsWrite | None = None,
                 package_load_callback: Callable[[Package], Any] | None = None,
                 prune_unfailed: bool = True,
                 suppress_passive: bool = False,
                 print_stats: bool = False) -> None:
        self.package_requests = list(package_requests)
        self.package_paths = package_paths
        self.context = context
        self.package_filter = package_filter
        self.package_orderers = package_orderers
        self.callback = callback
        self.building = building
        self.optimised = optimised
        self.verbosity = verbosity
        self.buf = buf
        self.package_load_callback = package_load_callback
        self.prune_unfailed = prune_unfailed
        self.suppress_passive = suppress_passive
        self.print_stats = print_stats

        # Solver-compatible state read by Resolver._solver_to_dict.
        self.solve_time: float | None = None
        self.load_time: float | None = None
        self.abort_reason: str | None = None
        self.callback_return: SolverCallbackReturn | None = None
        self.solve_begun: bool = False

        self._status: SolverStatus = SolverStatus.pending
        self._resolved_packages: list[PackageVariant] | None = None
        self._resolved_ephemerals: list[Requirement] | None = None
        self._failure_description: str = ""
        self._num_iterations: int = 0

    @property
    def status(self) -> SolverStatus:
        return self._status

    @property
    def num_solves(self) -> int:
        return self._num_iterations

    @property
    def num_fails(self) -> int:
        return 1 if self._status == SolverStatus.failed else 0

    @property
    def cyclic_fail(self) -> bool:
        return False

    @property
    def resolved_packages(self) -> list[PackageVariant] | None:
        if self._status != SolverStatus.solved:
            return None
        return self._resolved_packages

    @property
    def resolved_ephemerals(self) -> list[Requirement] | None:
        if self._status != SolverStatus.solved:
            return None
        return self._resolved_ephemerals

    def failure_description(self, failure_index: int | None = None) -> str:
        return self._failure_description

    def failure_reason(self, failure_index: int | None = None):
        # pyrer returns a free-form description; the structured FailureReason
        # objects are not reconstructed.
        return None

    def failure_packages(self, failure_index: int | None = None):
        return None

    def get_graph(self) -> digraph:
        """Return a minimal graph; pyrer does not expose the resolve graph."""
        g = digraph()
        if self._resolved_packages:
            for pv in self._resolved_packages:
                node = str(pv)
                if not g.has_node(node):
                    g.add_node(node)
        return g

    def reset(self) -> None:
        self.solve_begun = False
        self._status = SolverStatus.pending
        self._resolved_packages = None
        self._resolved_ephemerals = None
        self._failure_description = ""
        self._num_iterations = 0

    def solve(self) -> None:
        if self.solve_begun:
            raise RezSystemError(
                "cannot run solve() on a solve that has already been started"
            )
        self.solve_begun = True

        pyrer = _import_pyrer()

        t1 = time.time()
        pt1 = package_repo_stats.package_load_time

        try:
            packages = self._collect_packages(pyrer)
            request_strs = [str(r) for r in self.package_requests]

            # Mirror config.variant_select_mode. Import locally to avoid a
            # cycle at module load time.
            from rez.config import config
            mode = getattr(config, "variant_select_mode", None) \
                or "version_priority"

            result = pyrer.solve(
                request_strs,
                packages,
                variant_select_mode=mode,
            )
        finally:
            self.load_time = package_repo_stats.package_load_time - pt1
            self.solve_time = time.time() - t1

        self._num_iterations = int(getattr(result, "num_iterations", 0) or 0)
        self._consume_result(result)

    @property
    def solve_stats(self) -> dict[str, dict[str, Any]]:
        global_stats = {
            "num_solves": self.num_solves,
            "num_fails": self.num_fails,
            "solve_time": self.solve_time,
            "load_time": self.load_time,
        }
        return {
            "global": global_stats,
            "extractions": {},
            "intersections": {},
            "reductions": {},
            "backend": "pyrer",
        }

    def _collect_packages(self, pyrer: Any) -> list[Any]:
        """BFS over package families reachable from the request and feed
        :class:`pyrer.PackageData` instances back to the caller.
        """
        out: list[Any] = []
        seen: set[str] = set()
        queue: list[str] = []

        def _enqueue(name: str) -> None:
            if not name or name.startswith('.'):
                return
            if name in seen:
                return
            seen.add(name)
            queue.append(name)

        for req in self.package_requests:
            if not req.conflict:
                _enqueue(req.name)

        while queue:
            name = queue.pop(0)
            for pkg in iter_packages(name, paths=self.package_paths):
                if self.package_filter is not None and \
                        self.package_filter.excludes(pkg):
                    continue
                if self.package_load_callback is not None:
                    self.package_load_callback(pkg)
                out.append(pyrer.PackageData.from_rez(pkg))
                for dep_name in _gather_dependency_families(pkg):
                    _enqueue(dep_name)
        return out

    def _consume_result(self, result: Any) -> None:
        status_str = getattr(result, "status", "error")
        if status_str == "solved":
            self._status = SolverStatus.solved
            self._resolved_packages = [
                self._make_package_variant(rv)
                for rv in result.resolved_packages
            ]
            # `resolved_ephemerals` was added in pyrer 0.1.0-rc.7; older
            # builds lack the attribute and degrade to an empty list.
            ephemeral_strs = getattr(result, "resolved_ephemerals", None) or []
            self._resolved_ephemerals = [Requirement(s) for s in ephemeral_strs]
        elif status_str == "failed":
            self._status = SolverStatus.failed
            self._failure_description = result.failure_description or ""
        else:
            self._status = SolverStatus.failed
            self._failure_description = (
                getattr(result, "failure_description", None)
                or "rer solver reported status %r" % status_str
            )

    def _make_package_variant(self, rv: Any) -> PackageVariant:
        pkg = get_package(rv.name, rv.version, paths=self.package_paths)
        if pkg is None:
            raise RezSystemError(
                "rer solver resolved package %s-%s, but it could not be "
                "loaded from package_paths %r"
                % (rv.name, rv.version, self.package_paths)
            )
        variant = pkg.get_variant(rv.variant_index)
        if variant is None:
            raise RezSystemError(
                "rer solver resolved variant %s-%s[%s], but it could not "
                "be loaded" % (rv.name, rv.version, rv.variant_index)
            )
        return PackageVariant(variant, building=self.building)


def _gather_dependency_families(pkg: Package) -> set[str]:
    names: set[str] = set()
    for req in (pkg.requires or []):
        names.add(req.name)
    for variant in (pkg.variants or []):
        for req in (variant or []):
            names.add(req.name)
    return names
