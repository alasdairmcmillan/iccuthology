/**
 * R2 read helpers. The Worker resolves the current epoch via `latest.json`
 * at the bucket root, then reads `snapshots/{epoch}/...` (DEPLOY-CONTRACTS.md
 * §2, §6). Read-only -- no writes happen in the request path.
 */

export interface Env {
  SNAPSHOTS: R2Bucket;
  ASSETS: Fetcher;
}

export interface LatestPointer {
  epoch: string;
  created_at?: string;
}

/** Fetch and JSON-parse an R2 object; null if it doesn't exist. */
export async function getJson<T>(env: Env, key: string): Promise<T | null> {
  const obj = await env.SNAPSHOTS.get(key);
  if (obj === null) return null;
  return (await obj.json()) as T;
}

export interface R2Bytes {
  body: ArrayBuffer;
  etag: string;
  httpMetadata?: R2HTTPMetadata;
}

/** Fetch an R2 object's raw bytes; null if it doesn't exist. */
export async function getBytes(env: Env, key: string): Promise<R2Bytes | null> {
  const obj = await env.SNAPSHOTS.get(key);
  if (obj === null) return null;
  return { body: await obj.arrayBuffer(), etag: obj.httpEtag, httpMetadata: obj.httpMetadata };
}

/** Resolve the current epoch from the root `latest.json` pointer, or null if unpublished. */
export async function resolveEpoch(env: Env): Promise<string | null> {
  const latest = await getJson<LatestPointer>(env, "latest.json");
  return latest?.epoch ?? null;
}

/** Build the R2 key for a path under the current epoch's snapshot prefix. */
export function snapshotKey(epoch: string, path: string): string {
  return `snapshots/${epoch}/${path}`;
}
