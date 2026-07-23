import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import { resolve } from "path";

// Build separado del atlas: la SPA Lab la sirve playground.py bajo /lab/ (:8898).
export default defineConfig({
  plugins: [react()],
  base: "/lab/",
  build: {
    outDir: "dist-lab",
    emptyOutDir: true,
    rollupOptions: { input: resolve(__dirname, "lab.html") },
  },
});
