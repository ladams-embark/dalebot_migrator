"""The wizard steps, in order: connect, scope, select, plan, run, results.

Each module exposes `render(state)` and `gate(state) -> list[Blocker]`; see
`wdmigrator.ui.app` for how they're driven. Resolve / Conflicts / Confirm /
Execute still exist as implementation modules that Plan and Run compose.
"""