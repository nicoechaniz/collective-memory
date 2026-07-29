declare const __MAPA_APP_MODE__: "atlas" | "lab";

interface ImportMetaEnv {
  readonly VITE_ATLAS_ORIGIN?: string;
  readonly VITE_LAB_ORIGIN?: string;
  readonly VITE_LAB_STRUCTURAL_ONLY?: string;
  readonly VITE_LAB_STRUCTURAL_NOTICE?: string;
  readonly VITE_UI_LOCALE?: "es" | "en";
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}
