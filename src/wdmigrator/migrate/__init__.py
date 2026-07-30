"""Dependency ordering and destination writes.

Steps 3-4 of the build plan (docs/START_HERE.md) — not yet implemented.
Planned: ordering.build_dag(), ordering.topological_sort(),
         ordering.substitute_wids(), writer.put_calculated_field()

Every writer function defaults to dry_run=True.
"""
