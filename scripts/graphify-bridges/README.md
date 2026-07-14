# graphify-bridges — HTTP contract inventory for this repo

`inventories/bridge_ow.json` lists the FastAPI inbound endpoints
(full paths, include_router prefixes resolved). It feeds the cross-repo
bridge edges of the ecosystem knowledge graph.

- Canonical tooling & runbook: `app.bazard.run/scripts/graphify-bridges/`
- Schema: `[{file, method, path, symbol}]`, dynamic segments as `{param}`.
- Snapshot: regenerate when routes change.
