# NEXUS Observatory — web app

A private, installable (PWA) web app over the read-only Observatory API
(`observatory/api.py`). Next.js App Router + TypeScript + Tailwind, built as a
**static export**. The server never runs Node: the build output is committed to
`observatory/static/` and served by the API process itself.

## Rules this app keeps

- Real data only. No mock or placeholder data; every panel has an explicit
  empty state.
- No runtime calls to anything but its own origin. Fonts are self-hosted
  (`@fontsource-variable/*`, bundled at build). No analytics.
  `NEXT_TELEMETRY_DISABLED=1` for every script. The API's CSP enforces
  `connect-src 'self'` and `worker-src 'none'`.
- No service worker and no data caching: a reload always starts empty, so
  stale trading data can never look live. HTML is served `no-store`.
- The token is typed in on first visit and kept only in that browser's
  `localStorage`; it is sent as `Authorization: Bearer` on REST and as
  `?token=` on the WebSocket (the API's access log is off). "forget token"
  removes it.

## Build (Node >= 20.9; developed on Node 24)

From the repository root:

```bash
cd observatory/web
export NEXT_TELEMETRY_DISABLED=1
npm ci                      # exact versions from package-lock.json
npm run lint                # eslint
npx tsc --noEmit            # type check
npm test                    # vitest: lib/logic.test.ts
npm run build               # next build -> out/
rm -rf ../static && cp -R out ../static    # (same as: npm run publish-static)
```

Then commit `observatory/static/`. The server needs only Python.

`npm run icons` regenerates the PWA icons in `public/` (dependency-free PNG
encoder); their output is committed, and the build does not run it.

## Run locally

```bash
# from the repository root, with the read-only role from ops/dev/local_readonly_roles.sql
export OBSERVATORY_DATABASE_URL='postgresql://observatory@/nexus_prodcopy'
export OBSERVATORY_TOKEN="$(openssl rand -hex 32)"   # paste it into the app once
python3 -m observatory.api                          # http://127.0.0.1:8787
```

`npm run dev` works for UI work only if the API is reachable on the same
origin; production always uses the static build served by the API.

## Layout

- `app/` — routes: `/` Command, `/agents` Agents, `/trades` Trades.
- `components/` — shell (token gate, status bar, stale banner, nav) and screens.
- `lib/logic.ts` — every status rule (staleness, AI light, countdown, R
  formatting, marker clusters) as pure functions, tested in `lib/logic.test.ts`.
- `lib/data.tsx` — REST polling (10 s) and the `/ws/events` stream with
  exponential-backoff reconnect.
