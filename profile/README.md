# RetireGolden

**Free, private, open-source retirement planning — no login, no server, no data leaving your browser.**

We believe everyone should be able to plan their retirement without handing their financial life to a third party. Everything we build starts from that idea: your data stays on your device, the code is open for anyone to inspect, and the core planning tools are free.

🌐 [retiregolden.org](https://retiregolden.org) · ✉️ [info@retiregolden.org](mailto:info@retiregolden.org)

---

## 🧭 Our open-source projects

### [RetireGolden](https://github.com/RetireGolden/RetireGolden) — the planner
Privacy-first retirement planning that runs entirely in your browser as a PWA. Monte Carlo simulation, Social Security modeling, and tax-aware planning — with no accounts, no server, and no data leaving your device. TypeScript + React. Licensed AGPL-3.0-only.

### [RetireGolden-MCP](https://github.com/RetireGolden/RetireGolden-MCP) — the AI connector
A headless [Model Context Protocol](https://modelcontextprotocol.io) server for the [`@retiregolden/engine`](https://www.npmjs.com/package/@retiregolden/engine) calculator. Connect Claude Desktop, Claude Code, Cursor, or any MCP client and call typed tools for plan building, projections, Monte Carlo, and optimization — all in memory, read-only with respect to your finances. Try it with `npx @retiregolden/mcp`.

### [EntitleKit](https://github.com/RetireGolden/entitlekit) — authorization boundary
Provider-neutral roles, entitlements, and permissions for SaaS applications. Auth providers answer *who someone is*; billing providers report *what they purchased*; EntitleKit is the small, trustworthy boundary that combines those facts and decides *what a person may do*. MIT licensed, early `0.x` development.

### [Support Desk](https://github.com/RetireGolden/support-desk) — support queue
A composable, open-source support queue for web and email: authenticated ticketing, a central staff queue, threaded web and email replies, with provider-neutral identity, mail, and storage boundaries. MIT licensed, early `0.x` development.

---

## 📦 On npm

| Package | What it is |
|---|---|
| [`@retiregolden/engine`](https://www.npmjs.com/package/@retiregolden/engine) | The retirement-planning calculation engine |
| [`@retiregolden/mcp`](https://www.npmjs.com/package/@retiregolden/mcp) | MCP server exposing the engine to AI clients |

---

## 🤝 Contributing

Issues, discussions, and pull requests are welcome on any of our public repositories. Each repo carries its own contributing guidelines and license — the planner and engine are AGPL-3.0-only, while our infrastructure libraries are MIT.

## ⚖️ Disclaimer

Our tools are for education and decision support only — they are **not** tax, legal, investment, or financial advice. See the disclaimer in each repository for details.
