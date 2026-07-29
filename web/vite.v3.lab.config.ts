import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import { resolve } from "path";

export default defineConfig({
  plugins: [react()],
  base: "/lab-v3/",
  define: { __MAPA_APP_MODE__: JSON.stringify("lab") },
  build: {
    outDir: "dist-v3-lab",
    emptyOutDir: true,
    rollupOptions: { input: { index: resolve(__dirname, "v3-lab.html") } },
  },
});
