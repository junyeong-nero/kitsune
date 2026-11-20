# Philosophy

Two principles decide Kitsune's trade-offs. When a feature conflicts with them, the principles win.

## 1. The user's browser belongs to the user

The agent is a guest in a browser someone is using.

- Agent work opens in background tabs; the user's view stays where it is.
- User tabs, focus and input are off-limits unless the user allows otherwise.
- Refuse with guidance rather than silently act somewhere else.
- Don't buy speed with interference, and document where protections have limits.

## 2. Fewer, sharper tools

Every tool and description line costs agent context on every call.

- Keep the default surface small and each contract precise.
- Enforce limits (approvals, protected tabs) in the system, not in prompts.
- Report what happened (dispatched, observed or unknown) and let the agent decide.
- Add a tool or field only when it removes more agent work than it adds.
