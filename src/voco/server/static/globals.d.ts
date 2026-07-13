// Ambient types for the buildless client (SPEC-WORKBENCH §7).
// The daemon injects window.__VOCO__ into the served shell; vendored UMD
// modules re-export a global. Declared here so tsc --checkJs stays strict
// without a build step or @types install.

interface Window {
  __VOCO__?: { wb?: string };
  __VOCO_WB__?: string;
}

declare module "./vendor/marked.mjs" {
  export const marked: {
    parse(md: string, opts?: { breaks?: boolean; gfm?: boolean }): string;
  };
}

declare module "./vendor/purify.mjs" {
  const purify: { sanitize(html: string, cfg?: unknown): string };
  export default purify;
}
