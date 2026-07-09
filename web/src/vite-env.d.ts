/// <reference types="vite/client" />

interface ImportMetaEnv {
  /** API base URL. "" (default) = same origin as the served static assets. */
  readonly VITE_API_BASE?: string;
  /** "true" force fixtures, "false" force network, unset = dev→fixtures/prod→API. */
  readonly VITE_USE_FIXTURES?: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}
