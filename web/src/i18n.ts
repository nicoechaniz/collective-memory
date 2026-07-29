export const ENGLISH_UI = import.meta.env.VITE_UI_LOCALE === "en";
export const UI_LOCALE = ENGLISH_UI ? "en-US" : "es-AR";

export function t(spanish: string, english: string): string {
  return ENGLISH_UI ? english : spanish;
}
