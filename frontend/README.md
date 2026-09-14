# RK3588 Demo Hub Frontend

Vue 3 + Vite dashboard for the Demo Hub backend.

```bash
npm install
npm run dev
```

The Vite development server listens on `http://localhost:5173` and proxies `/api` plus WebSocket traffic to `http://127.0.0.1:8080`.

Production build:

```bash
npm run build
```

Configuration:

- `VITE_API_BASE`: REST prefix, defaults to `/api/v1`.
- `VITE_WS_URL`: complete WebSocket URL; by default it uses the current origin and `/api/v1/stream`.

