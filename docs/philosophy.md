# Philosophy

Two principles decide Kitsune's trade-offs. When a feature, default, or fix
conflicts with them, the principles win.

## 1. The user's browser belongs to the user

Kitsune runs inside a browser someone is actively using. The agent is a guest
in that workspace, not its owner.

**We do:**

- Open agent work in background tabs and leave the user's view where it is.
- Hold writes to a tab the user is using instead of racing their input.
- Treat user-owned tabs, focus, and input as off-limits unless the user
  authorizes otherwise.
- Prefer refusing with guidance over silently acting somewhere else.

**We don't:**

- Trade workspace interference for task speed or benchmark wins.
- Promise "never interrupts." Where a protection has limits, we document them.

## 2. Fewer, sharper tools

Every tool, parameter, and description line is paid for in agent context on
every call. A larger catalog is a cost, not a feature.

**We do:**

- Keep the default surface small and make each tool's contract precise.
- Enforce delegation limits (`read`, `draft`, `act`) in the system, not in
  prompts.
- Report what happened—dispatched, observed, or unknown—and leave the
  decision to the agent.
- Add a tool or field only when it removes more agent work than it adds.

**We don't:**

- Compete on tool count or copy adjacent tools' catalogs.
- Fix agent misuse by adding more description text when a simpler contract
  would prevent it.

---

For market context and competitor comparison, see [Positioning](positioning.md).
