// Talks to a Leafguard wallet server (pqv/wallet.py) running on the wallet computer. The keys and the signing stay
// there: this app only holds the server URL and the wallet's pairing code, sent as a Bearer token on every call.
import { Platform } from "react-native";
import * as SecureStore from "expo-secure-store";

export type Conn = { url: string; code: string };

export type Key = { index: number; leaves: number; remaining: number; renewals: number };
export type Contact = { name: string; address: string; vault?: boolean };
export type VaultMember = { wallet: string; index: number; joined: boolean; me: boolean };
export type VaultSummary = {
  id: string; name: string; status: string; address: string | null; creator: string; members: VaultMember[];
  invited: boolean; balanceWei: string | null; pending: number; awaitingMe: number;
};
export type State = {
  name: string;
  remote: boolean;
  network: { rpc: string; mempool: string; connected: boolean; chainId?: number; block?: number; infra?: boolean; error?: string };
  account: { address: string; balanceWei: string } | null;
  staleAccount?: string;
  keys: Key[];
  contacts: Contact[];
  vaults?: VaultSummary[];
  wallets?: string[];
  rotateAt: number;
};
export type Activity = { direction: "in" | "out"; counterparty: string; name: string | null; wei: string; block: number; txHash: string };
export type PlanTx = { what: "payment" | "renew"; guardians: number[]; kinds: string[]; newStorage: number; gas: number };
export type Quote = { feeWei: string; gas: number; plan: PlanTx[]; maxSendWei: string };
export type Approval = { index: number; wallet: string; kind: string };
export type Proposal = {
  id: string; proposer: string; to: string; wei: string; status: string; txHash?: string; block?: number; error?: string;
  approvals: Approval[];
};
export type VaultDetail = VaultSummary & {
  myIndex: number | null; proposals: Proposal[]; activity: Activity[]; myKey: { remaining: number; leaves: number } | null;
};
export type JobEvent = { type: string; text?: string; ok?: boolean; block?: number };
export type Job = { id: string; state: "running" | "done" | "error"; events: JobEvent[]; error?: string };

export class ApiError extends Error {
  constructor(message: string, public status = 0, public pair = false, public offline = false) { super(message); }
}

const STORE_KEY = "pqwallet.conn";
let conn: Conn | null = null;

// The phone keeps the pairing code in the OS keystore; the web build (for development) in localStorage.
const store = Platform.OS === "web" ? {
  getItemAsync: async (k: string) => globalThis.localStorage?.getItem(k) ?? null,
  setItemAsync: async (k: string, v: string) => globalThis.localStorage?.setItem(k, v),
  deleteItemAsync: async (k: string) => globalThis.localStorage?.removeItem(k),
} : SecureStore;

export const current = () => conn;

export async function loadConn(): Promise<Conn | null> {
  try {
    const raw = await store.getItemAsync(STORE_KEY);
    conn = raw ? (JSON.parse(raw) as Conn) : null;
  } catch {
    conn = null;
  }
  return conn;
}

export async function saveConn(c: Conn | null) {
  conn = c;
  if (c) await store.setItemAsync(STORE_KEY, JSON.stringify(c));
  else await store.deleteItemAsync(STORE_KEY);
}

/** "abc.ngrok-free.app" -> "https://abc.ngrok-free.app"; keeps a path ("host/bob" for a wallet behind the gateway),
 *  drops the query, the fragment and a trailing slash. */
export function normalizeUrl(s: string): string | null {
  s = s.trim();
  if (!s) return null;
  if (!/^https?:\/\//i.test(s)) s = "https://" + s;
  const m = s.match(/^(https?:\/\/[^/?#\s]+)(\/[^?#\s]*)?/i);
  return m ? (m[1].replace(/^https?/i, (x) => x.toLowerCase()) + (m[2] || "")).replace(/\/+$/, "") : null;
}

export const normalizeCode = (s: string) => s.toUpperCase().replace(/[^A-Z0-9]/g, "");

/** The wallet computer's pairing QR: "<url>/#pair=<code>" (pqv/wallet.py pair_info), "<url>/bob/#pair=<code>"
 *  behind the gateway (pqv/gateway.py). */
export function parsePairLink(s: string): Conn | null {
  const m = s.trim().match(/^(https?:\/\/[^?#\s]+)[^#]*#pair=([A-Za-z0-9-]+)/i);
  if (!m) return null;
  const url = normalizeUrl(m[1]);
  return url ? { url, code: normalizeCode(m[2]) } : null;
}

export async function api<T = any>(path: string, body?: object, c: Conn | null = conn): Promise<T> {
  if (!c) throw new ApiError("Not connected to a wallet.", 0, true);
  const headers: Record<string, string> = {
    accept: "application/json",
    authorization: "Bearer " + c.code,
    "ngrok-skip-browser-warning": "1", // ngrok's free tunnels otherwise answer browsers with an HTML warning page
  };
  if (body) headers["content-type"] = "application/json";
  const ctl = new AbortController();
  const timer = setTimeout(() => ctl.abort(), 20000);
  let r: Response;
  try {
    r = await fetch(c.url + path, { method: body ? "POST" : "GET", headers, body: body ? JSON.stringify(body) : undefined, signal: ctl.signal });
  } catch (e) {
    throw new ApiError(`Cannot reach ${c.url}.`, 0, false, true);
  } finally {
    clearTimeout(timer);
  }
  const text = await r.text();
  let j: any = {};
  try { j = text ? JSON.parse(text) : {}; } catch {
    // an HTML page instead of JSON: usually a tunnel's error page (ngrok offline, wallet not running)
    throw new ApiError(r.status === 404 || r.status >= 500 ? `The tunnel at ${c.url} is not reaching a wallet.` : `Unexpected answer from ${c.url} (${r.status}).`, r.status, false, true);
  }
  if (!r.ok) throw new ApiError(j.error || `Request failed (${r.status}).`, r.status, !!j.pair);
  return j as T;
}

export const sleep = (ms: number) => new Promise((res) => setTimeout(res, ms));
