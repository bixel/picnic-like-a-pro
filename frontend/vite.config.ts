import path from "node:path";

import tailwindcss from "@tailwindcss/vite";
import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";

// `npm run dev` serves the UI on :5173 and forwards /api to `uv run web`.
export default defineConfig({
  plugins: [react(), tailwindcss()],
  resolve: { alias: { "@": path.resolve(import.meta.dirname, "src") } },
  server: {
    proxy: { "/api": process.env.VITE_API_TARGET ?? "http://127.0.0.1:8080" },
  },
  test: { environment: "node" },
});
