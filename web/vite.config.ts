import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  base: "/atlas/graph/",
  build: {
    outDir: "dist-atlas-graph",
    emptyOutDir: true,
  },
});
