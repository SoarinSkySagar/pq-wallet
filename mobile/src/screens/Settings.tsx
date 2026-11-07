import React, { useState } from "react";
import { Text, View } from "react-native";
import { api, normalizeUrl, type State } from "../api";
import { cap, hexOf, num, short } from "../format";
import { HERE, useWallet } from "../store";
import { Avatar, Btn, C, Field, H2, Note, Screen, T, s } from "../ui";

/** The node RPC + alt mempool the wallet computer uses (POST /api/network), and this phone's pairing. */
export function Settings({ forced }: { forced?: boolean }) {
  const w = useWallet();
  const n = w.S.network;
  const [rpc, setRpc] = useState(n.rpc);
  const [mp, setMp] = useState(n.mempool);
  const [err, setErr] = useState("");
  const [ok, setOk] = useState("");
  const [busy, setBusy] = useState(false);
  const [url, setUrl] = useState(w.conn.url);
  const [urlMsg, setUrlMsg] = useState<{ text: string; bad?: boolean } | null>(null);
  const [moving, setMoving] = useState(false);

  async function move() {
    const u = normalizeUrl(url);
    if (!u) return setUrlMsg({ text: "Enter a URL such as https://abcd-1234.ngrok-free.app", bad: true });
    setMoving(true); setUrlMsg(null);
    try { await w.moveTo(u); setUrlMsg({ text: "Saved." }); } catch (e) { setUrlMsg({ text: (e as Error).message, bad: true }); }
    setMoving(false);
  }

  async function save() {
    setBusy(true); setErr(""); setOk("");
    try {
      const r = await api<{ chainId: number }>("/api/network", { ethrex: rpc.trim(), sidecar: mp.trim() });
      setOk(`Connected to chain ${r.chainId}.`);
      await w.refresh();
    } catch (e) { setErr((e as Error).message); }
    setBusy(false);
  }
  return (
    <Screen>
      <H2>{forced ? "Network" : "Settings"}</H2>
      <Text style={st.section}>Network (used by the wallet computer)</Text>
      <T small style={{ color: n.connected ? C.plus : C.bad, marginBottom: 12 }}>
        {n.connected ? `Connected to chain ${n.chainId}, block ${num(n.block || 0)}.` : `The wallet cannot reach a node at ${n.rpc}.`}
      </T>
      <Field label="Node RPC URL" value={rpc} onChangeText={setRpc} autoCapitalize="none" keyboardType="url" style={s.mono} />
      <Field label="Alt mempool URL" value={mp} onChangeText={setMp} autoCapitalize="none" keyboardType="url" style={s.mono}
        hint="Payments go to this ERC-8421 mempool. It must allow staked accounts more than 100,000 validation gas." />
      <T small muted style={{ marginBottom: 12 }}>The wallet computer uses these URLs (the keys sign there), so they must work from
        that computer: for the local devnet, keep the 127.0.0.1 addresses.</T>
      {err ? <T small style={{ color: C.bad, marginBottom: 12 }}>{err}</T> : null}
      {ok ? <T small style={{ color: C.plus, marginBottom: 12 }}>{ok}</T> : null}
      <Btn title="Save and connect" onPress={save} busy={busy} />

      <Text style={[st.section, { marginTop: 30 }]}>Wallet computer</Text>
      <Field label="Wallet URL" value={url} onChangeText={setUrl} autoCapitalize="none" keyboardType="url" style={s.mono}
        hint="Where this phone reaches the wallet: the ngrok tunnel's URL. A restarted tunnel gets a new one unless it uses a fixed ngrok domain." />
      {urlMsg ? <T small style={{ color: urlMsg.bad ? C.bad : C.plus, marginBottom: 12 }}>{urlMsg.text}</T> : null}
      <Btn ghost title="Save wallet URL" onPress={move} busy={moving} disabled={normalizeUrl(url) === w.conn.url} />

      <Text style={[st.section, { marginTop: 30 }]}>This phone</Text>
      <View style={[s.card, { flexDirection: "row", alignItems: "center", gap: 12 }]}>
        <Avatar addr={w.S.account?.address || hexOf(w.S.name)} />
        <View style={{ flex: 1 }}>
          <T bold>{cap(w.S.name)}'s wallet</T>
          <T small muted style={s.mono} selectable>{w.conn.url.replace(/^https?:\/\//, "")}</T>
          {w.S.account ? <T small muted style={s.mono}>{short(w.S.account.address)}</T> : null}
        </View>
      </View>
      <Btn ghost danger title="Disconnect this phone" onPress={w.disconnect} />
      <Note>Paired with a code. The keys are {HERE}; disconnecting only forgets the URL and the code here.</Note>
    </Screen>
  );
}

/** First run of a wallet with no account yet on this chain. */
export function Setup() {
  const w = useWallet();
  const S: State = w.S;
  const create = () => w.push({ name: "job", job: { title: `Creating ${cap(S.name)}'s wallet`, path: "/api/create", body: {}, doneText: "Your wallet is ready.", back: "home" } });
  return (
    <Screen center>
      <View style={{ alignItems: "center" }}>
        <Avatar addr={hexOf(S.name)} size={72} />
        <H2 style={{ marginTop: 14, marginBottom: 8 }}>Create {cap(S.name)}'s wallet</H2>
        <T muted center style={{ marginBottom: 16 }}>Your account will be a smart contract guarded by three post-quantum keys kept {HERE}.
          Any two of them sign each payment, so there is no seed phrase to write down.</T>
        {S.staleAccount ? <T small center style={{ color: C.warn, marginBottom: 12 }}>This wallet's previous account ({short(S.staleAccount)}) is not on
          the connected network, which was probably restarted. Creating a wallet makes a new account; the old keys are archived.</T> : null}
        {!S.network.infra ? <T small center style={{ color: C.bad, marginBottom: 12 }}>This network has no Leafguard contracts yet. Run ./demo.sh on the wallet computer first.</T> : null}
      </View>
      <Btn title="Create wallet" onPress={create} disabled={!S.network.infra} />
      <T small muted center style={{ marginTop: 10 }}>Creating the account and adding 1 test ETH takes about 20 seconds.</T>
      <Btn ghost title="Settings" onPress={() => w.push({ name: "settings" })} style={{ marginTop: 20 }} />
    </Screen>
  );
}

const st = { section: { fontSize: 13, fontWeight: "600" as const, color: C.slate, textTransform: "uppercase" as const, letterSpacing: 0.6, marginBottom: 10 } };
