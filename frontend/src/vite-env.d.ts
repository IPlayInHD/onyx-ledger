/// <reference types="vite/client" />

interface ImportMetaEnv {
  /** PUBLIC backend origin. Never a secret — everything here ships to the browser. */
  readonly VITE_API_BASE_URL?: string
}
interface ImportMeta {
  readonly env: ImportMetaEnv
}
