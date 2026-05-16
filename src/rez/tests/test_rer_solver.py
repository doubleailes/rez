# SPDX-License-Identifier: Apache-2.0
# Copyright Contributors to the Rez Project


"""
Tests for the optional rust-backed solver wired up via ``rez.rer_solver``.
"""
import unittest

from rez.solver import SolverStatus
from rez.tests.util import TestBase
from rez.version import Requirement

try:
    import pyrer  # noqa: F401
    _HAVE_PYRER = True
except ImportError:
    _HAVE_PYRER = False


@unittest.skipUnless(_HAVE_PYRER, "pyrer is not installed")
class TestRerSolver(TestBase):
    @classmethod
    def setUpClass(cls) -> None:
        packages_path = cls.data_path("solver", "packages")
        cls.packages_path = [packages_path]
        cls.settings = dict(
            packages_path=cls.packages_path,
            package_filter=None,
        )

    def _make_solver(self, request):
        from rez.rer_solver import RerSolver
        reqs = [Requirement(x) for x in request]
        return RerSolver(reqs, self.packages_path)

    def _resolve_names(self, solver):
        return [
            (pv.name, str(pv.version), pv.index)
            for pv in (solver.resolved_packages or [])
        ]

    def test_empty_request_solves_to_nothing(self) -> None:
        s = self._make_solver([])
        s.solve()
        self.assertEqual(s.status, SolverStatus.solved)
        self.assertEqual(self._resolve_names(s), [])

    def test_single_package(self) -> None:
        s = self._make_solver(["python"])
        s.solve()
        self.assertEqual(s.status, SolverStatus.solved)
        self.assertEqual(self._resolve_names(s), [("python", "2.7.0", None)])

    def test_versioned_request(self) -> None:
        s = self._make_solver(["python-2.6"])
        s.solve()
        self.assertEqual(s.status, SolverStatus.solved)
        self.assertEqual(self._resolve_names(s), [("python", "2.6.8", None)])

    def test_variant_selection(self) -> None:
        s = self._make_solver(["pyvariants"])
        s.solve()
        self.assertEqual(s.status, SolverStatus.solved)
        resolved = {(name, ver): idx for (name, ver, idx) in self._resolve_names(s)}
        self.assertIn(("pyvariants", "2"), resolved)
        # the highest-priority variant requires python-2.7.0
        self.assertEqual(resolved[("pyvariants", "2")], 0)
        self.assertEqual(resolved[("python", "2.7.0")], None)

    def test_conflict_request_fails(self) -> None:
        s = self._make_solver(["nada", "!nada"])
        s.solve()
        self.assertEqual(s.status, SolverStatus.failed)
        self.assertIsNone(s.resolved_packages)
        self.assertTrue(s.failure_description())

    def test_resolved_ephemerals(self) -> None:
        """pyrer >= 0.1.0-rc.7 surfaces resolved ephemeral ranges."""
        s = self._make_solver([".feature-1+<3", ".feature-2+"])
        s.solve()
        self.assertEqual(s.status, SolverStatus.solved)
        ephemerals = sorted(str(e) for e in (s.resolved_ephemerals or []))
        self.assertEqual(ephemerals, [".feature-2+<3"])

    def test_resolved_variants_carry_rez_handles(self) -> None:
        """Variants returned by the rer solver round-trip through the
        ``rez.packages.get_variant`` handle protocol used by Resolver."""
        from rez.packages import get_variant
        s = self._make_solver(["python"])
        s.solve()
        self.assertEqual(s.status, SolverStatus.solved)
        for pv in s.resolved_packages:
            v = get_variant(pv.handle)
            self.assertEqual(v.name, pv.name)
            self.assertEqual(str(v.version), str(pv.version))


@unittest.skipUnless(_HAVE_PYRER, "pyrer is not installed")
class TestResolverWithRerSolver(TestBase):
    """Verify that ``config.use_rer_solver`` routes Resolver through pyrer."""

    @classmethod
    def setUpClass(cls) -> None:
        packages_path = cls.data_path("solver", "packages")
        cls.packages_path = [packages_path]
        cls.settings = dict(
            packages_path=cls.packages_path,
            package_filter=None,
            resolve_caching=False,
            use_rer_solver=True,
        )

    def test_resolver_dispatch(self) -> None:
        from rez.resolver import Resolver, ResolverStatus
        reqs = [Requirement("python-2.6")]
        r = Resolver(context=None,
                     package_requests=reqs,
                     package_paths=self.packages_path,
                     caching=False)
        r.solve()
        self.assertEqual(r.status, ResolverStatus.solved)
        names = [(v.name, str(v.version)) for v in (r.resolved_packages or [])]
        self.assertEqual(names, [("python", "2.6.8")])


if __name__ == "__main__":
    unittest.main()
