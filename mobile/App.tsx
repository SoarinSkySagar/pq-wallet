// Leafguard: a phone app for a Leafguard wallet. The wallet itself (three JARDIN post-quantum keys, signing, the
// EIP-8141 frame transactions) runs on the wallet computer (pqv/wallet.py); this app reaches it over an HTTPS
// tunnel such as ngrok, paired with the wallet's pairing code. See mobile/README.md.
import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { ActivityIndicator, AppState, BackHandler, KeyboardAvoidingView, Platform, Pressable, StyleSheet, Text, View } from "react-native";
import { SafeAreaProvider, SafeAreaView } from "react-native-safe-area-context";
import { StatusBar } from "expo-status-bar";
import Svg, { Circle, Path } from "react-native-svg";
import { ApiError, api, loadConn, saveConn, type Activity, type Conn, type State } from "./src/api";
import { accentFor } from "./src/format";
import { Accent, Btn, C, Note, Screen, T } from "./src/ui";
import { WalletCtx, type Ctx, type Route, type Tab } from "./src/store";
import Connect from "./src/screens/Connect";
import Scan from "./src/screens/Scan";
import Job from "./src/screens/Job";
import { KeysTab, VaultsTab, WalletTab } from "./src/screens/Home";
import { Receive, Review, Send } from "./src/screens/Pay";
import { NewVault, Propose, Vault } from "./src/screens/Vaults";
import { Settings, Setup } from "./src/screens/Settings";

type Boot = { phase: "loading" } | { phase: "connect"; initial?: Partial<Conn>; message?: string } | { phase: "ready"; conn: Conn };

export default function App() {
  return (
    <SafeAreaProvider>
      <StatusBar style="dark" />
      <Root />
    </SafeAreaProvider>
  );
}

function Root() {
  const [boot, setBoot] = useState<Boot>({ phase: "loading" });
  const [S, setS] = useState<State | null>(null);
  const [A, setA] = useState<Activity[]>([]);
  const [offline, setOffline] = useState<string | null>(null);
  const [stack, setStack] = useState<Route[]>([{ name: "home" }]);
  const [tab, setTab] = useState<Tab>("wallet");
  const busy = useRef(false);

  useEffect(() => {
    loadConn().then((c) => setBoot(c ? { phase: "ready", conn: c } : { phase: "connect" }));
  }, []);

  const conn = boot.phase === "ready" ? boot.conn : null;

  const refresh = useCallback(async () => {
    if (!conn) return;
    try {
      const s = await api<State>("/api/state");
      setS(s); setOffline(null);
      if (s.account) { try { setA(await api<Activity[]>("/api/activity")); } catch { /* keep the last list */ } }
    } catch (e) {
      const x = e as ApiError;
      if (x.pair && x.status !== 429) {  // forget a rejected code, so polling does not count as more wrong guesses
        await saveConn(null);
        setS(null);
        setBoot({ phase: "connect", initial: { url: conn.url }, message: "The wallet no longer accepts this phone's pairing code. Enter the current one." });
      } else {
        setOffline(x.message);
      }
    }
  }, [conn]);

  useEffect(() => { if (conn) refresh(); }, [conn, refresh]);

  const top = stack[stack.length - 1];
  // keep balances and vault approvals fresh while the app is open on a screen that shows them
  useEffect(() => {
    if (!conn) return;
    const t = setInterval(() => {
      if (AppState.currentState === "active" && !busy.current && (top.name === "home" || !S)) refresh();
    }, 4000);
    const sub = AppState.addEventListener("change", (st) => { if (st === "active" && !busy.current) refresh(); });
    return () => { clearInterval(t); sub.remove(); };
  }, [conn, refresh, top.name, S]);

  const ctx: Ctx | null = useMemo(() => (S && conn ? {
    S, A, conn, refresh, tab, setTab,
    push: (r) => setStack((st) => [...st, r]),
    pop: () => setStack((st) => (st.length > 1 ? st.slice(0, -1) : st)),
    replace: (r) => setStack((st) => [...st.slice(0, -1), r]),
    reset: (r) => setStack(r ? [{ name: "home" }, r] : [{ name: "home" }]),
    setBusy: (b) => { busy.current = b; },
    moveTo: async (url) => {
      const c = { url, code: conn.code };
      let s: State;
      try { s = await api<State>("/api/state", undefined, c); } catch (e) {
        throw (e as ApiError).pair ? new Error("That URL does not accept this wallet's pairing code: it is probably another wallet.") : e;
      }
      if (s.name !== S.name) throw new Error(`That URL reaches ${s.name}'s wallet, not ${S.name}'s.`);
      await saveConn(c); setS(s); setBoot({ phase: "ready", conn: c });
    },
    disconnect: async () => { await saveConn(null); setS(null); setA([]); setStack([{ name: "home" }]); setBoot({ phase: "connect", initial: { url: conn.url } }); },
  } : null), [S, A, conn, refresh, tab]);

  // Android back button: go back a screen, never out of a running payment
  useEffect(() => {
    const sub = BackHandler.addEventListener("hardwareBackPress", () => {
      if (top.name === "job" && busy.current) return true;
      if (stack.length > 1) { setStack((st) => st.slice(0, -1)); return true; }
      if (tab !== "wallet") { setTab("wallet"); return true; }
      return false;
    });
    return () => sub.remove();
  }, [stack.length, top.name, tab]);

  const accent = S?.account ? accentFor(S.account.address) : C.acc;

  if (boot.phase === "loading") return <Splash />;
  if (boot.phase === "connect") {
    return (
      <Frame>
        <Connect initial={boot.initial} message={boot.message} onConnected={(c, s) => {
          setS(s); setOffline(null); setStack([{ name: "home" }]); setTab("wallet"); setBoot({ phase: "ready", conn: c });
        }} />
      </Frame>
    );
  }
  if (!ctx) {
    return (
      <Frame>
        {offline ? (
          <Screen center>
            <T bold center style={{ fontSize: 18, marginBottom: 8 }}>Cannot reach your wallet computer</T>
            <T muted center style={{ marginBottom: 20 }}>{offline} Check that the wallet runs (./demo.sh) and that the tunnel (./demo.sh tunnel) is up.</T>
            <Btn title="Try again" onPress={refresh} />
            <Btn ghost title="Change the wallet URL" style={{ marginTop: 10 }} onPress={() => setBoot({ phase: "connect", initial: conn ?? undefined,
              message: "A restarted ngrok tunnel has a new URL: enter it here, or scan the new QR code." })} />
            <Note>The keys are on the wallet computer, not on this phone, so nothing is lost while it is unreachable.</Note>
          </Screen>
        ) : <Splash />}
      </Frame>
    );
  }

  let body: React.ReactNode;
  let showNav = false;
  if (top.name === "scan") {
    return (
      <Accent.Provider value={accent}><WalletCtx.Provider value={ctx}>
        <View style={{ flex: 1, backgroundColor: "#000" }}><Scan hint={top.hint} onData={top.onData} onCancel={ctx.pop} /></View>
      </WalletCtx.Provider></Accent.Provider>
    );
  }
  if (!S!.network.connected && top.name !== "settings") body = <Settings forced />;
  else if (!S!.account && top.name !== "job" && top.name !== "settings") body = <Setup />;
  else {
    switch (top.name) {
      case "home": showNav = true; body = tab === "vaults" ? <VaultsTab /> : tab === "keys" ? <KeysTab /> : <WalletTab />; break;
      case "send": body = <Send to={top.to} />; break;
      case "review": body = <Review to={top.to} amount={top.amount} quote={top.quote} />; break;
      case "receive": body = <Receive />; break;
      case "job": body = <Job key={JSON.stringify(top.job)} job={top.job} />; break;
      case "vault": body = <Vault key={top.id} id={top.id} msg={top.msg} />; break;
      case "propose": body = <Propose id={top.id} />; break;
      case "newvault": body = <NewVault />; break;
      case "settings": body = <Settings />; break;
    }
  }
  const canBack = stack.length > 1 && !(top.name === "job" && busy.current);
  return (
    <Accent.Provider value={accent}>
      <WalletCtx.Provider value={ctx}>
        <Frame accent={accent}>
          <TopBar canBack={canBack} onBack={ctx.pop} onSettings={top.name === "settings" ? undefined : () => ctx.push({ name: "settings" })} />
          <View style={{ flex: 1 }}>{body}</View>
          {showNav ? <NavBar /> : null}
        </Frame>
      </WalletCtx.Provider>
    </Accent.Provider>
  );
}

function Frame({ children, accent }: { children: React.ReactNode; accent?: string }) {
  return (
    <SafeAreaView edges={["top", "bottom", "left", "right"]} style={{ flex: 1, backgroundColor: C.panel }}>
      <View style={{ height: 4, backgroundColor: accent || C.acc }} />
      <KeyboardAvoidingView style={{ flex: 1 }} behavior={Platform.OS === "ios" ? "padding" : undefined}>{children}</KeyboardAvoidingView>
    </SafeAreaView>
  );
}

const Splash = () => <View style={{ flex: 1, alignItems: "center", justifyContent: "center", backgroundColor: C.panel }}><ActivityIndicator color={C.acc} /></View>;

function TopBar({ canBack, onBack, onSettings }: { canBack: boolean; onBack: () => void; onSettings?: () => void }) {
  return (
    <WalletCtx.Consumer>{(w) => {
      const n = w!.S.network;
      return (
        <View style={st.top}>
          {canBack ? (
            <Pressable onPress={onBack} accessibilityRole="button" accessibilityLabel="Back" hitSlop={10} style={st.back}>
              <Text style={{ fontSize: 16, color: C.ink }}>← Back</Text>
            </Pressable>
          ) : (
            <View style={st.pill}>
              <View style={[st.dot, { backgroundColor: n.connected ? C.plus : C.bad }]} />
              <Text style={{ fontSize: 13, color: C.ink }}>{n.connected ? `PQ devnet, chain ${n.chainId}` : "Not connected"}</Text>
            </View>
          )}
          <View style={{ flex: 1 }} />
          {onSettings ? (
            <Pressable onPress={onSettings} accessibilityRole="button" accessibilityLabel="Settings" hitSlop={10} style={st.iconBtn}>
              <Svg width={20} height={20} viewBox="0 0 24 24" fill="none" stroke={C.ink} strokeWidth={1.8} strokeLinecap="round" strokeLinejoin="round">
                <Circle cx={12} cy={12} r={3} />
                <Path d="M19.4 15a1.7 1.7 0 0 0 .3 1.8l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.7 1.7 0 0 0-1.8-.3 1.7 1.7 0 0 0-1 1.5V21a2 2 0 1 1-4 0v-.1a1.7 1.7 0 0 0-1.1-1.5 1.7 1.7 0 0 0-1.8.3l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1a1.7 1.7 0 0 0 .3-1.8 1.7 1.7 0 0 0-1.5-1H3a2 2 0 1 1 0-4h.1a1.7 1.7 0 0 0 1.5-1.1 1.7 1.7 0 0 0-.3-1.8l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1a1.7 1.7 0 0 0 1.8.3H9a1.7 1.7 0 0 0 1-1.5V3a2 2 0 1 1 4 0v.1a1.7 1.7 0 0 0 1 1.5 1.7 1.7 0 0 0 1.8-.3l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.7 1.7 0 0 0-.3 1.8V9a1.7 1.7 0 0 0 1.5 1H21a2 2 0 1 1 0 4h-.1a1.7 1.7 0 0 0-1.5 1z" />
              </Svg>
            </Pressable>
          ) : null}
        </View>
      );
    }}</WalletCtx.Consumer>
  );
}

const NAV: [Tab, string, React.ReactNode][] = [
  ["wallet", "Wallet", <><Path d="M3 7.5A2.5 2.5 0 0 1 5.5 5h13A2.5 2.5 0 0 1 21 7.5v9a2.5 2.5 0 0 1-2.5 2.5h-13A2.5 2.5 0 0 1 3 16.5z" /><Path d="M15 12h3" /></>],
  ["vaults", "Vaults", <><Path d="M12 3l8 3v5.5c0 4.6-3.3 8.4-8 9.5-4.7-1.1-8-4.9-8-9.5V6z" /><Path d="M9 12l2 2 4-4" /></>],
  ["keys", "Keys", <><Circle cx={8} cy={15} r={4} /><Path d="M11 12l8-8M16 7l2 2M14 9l2 2" /></>],
];

function NavBar() {
  return (
    <WalletCtx.Consumer>{(w) => {
      const badge = (w!.S.vaults || []).reduce((k, v) => k + v.awaitingMe + (v.invited ? 1 : 0), 0);
      return (
        <Accent.Consumer>{(acc) => (
          <View style={st.nav}>
            {NAV.map(([k, label, icon]) => {
              const on = w!.tab === k;
              return (
                <Pressable key={k} onPress={() => w!.setTab(k)} accessibilityRole="tab" accessibilityState={{ selected: on }} style={st.navBtn}>
                  <Svg width={24} height={24} viewBox="0 0 24 24" fill="none" stroke={on ? acc : C.slate} strokeWidth={1.8} strokeLinecap="round" strokeLinejoin="round">{icon}</Svg>
                  <Text style={{ fontSize: 12, color: on ? acc : C.slate, fontWeight: on ? "600" : "400" }}>{label}</Text>
                  {k === "vaults" && badge ? <View style={st.badge}><Text style={{ color: "#fff", fontSize: 11, fontWeight: "700" }}>{badge}</Text></View> : null}
                </Pressable>
              );
            })}
          </View>
        )}</Accent.Consumer>
      );
    }}</WalletCtx.Consumer>
  );
}

const st = StyleSheet.create({
  top: { flexDirection: "row", alignItems: "center", paddingHorizontal: 16, paddingVertical: 10, borderBottomWidth: StyleSheet.hairlineWidth, borderBottomColor: C.rule },
  pill: { flexDirection: "row", alignItems: "center", gap: 7, paddingVertical: 5, paddingHorizontal: 11, borderRadius: 999, borderWidth: 1, borderColor: C.rule, backgroundColor: C.soft },
  dot: { width: 8, height: 8, borderRadius: 4 },
  back: { paddingVertical: 6, paddingRight: 10 },
  iconBtn: { padding: 6, borderRadius: 8, borderWidth: 1, borderColor: C.rule },
  nav: { flexDirection: "row", borderTopWidth: StyleSheet.hairlineWidth, borderTopColor: C.rule, backgroundColor: C.panel, paddingTop: 6, paddingBottom: 4 },
  navBtn: { flex: 1, alignItems: "center", gap: 2, paddingVertical: 4 },
  badge: { position: "absolute", top: 0, right: "28%", minWidth: 18, height: 18, borderRadius: 9, backgroundColor: C.bad, alignItems: "center", justifyContent: "center", paddingHorizontal: 4 },
});
